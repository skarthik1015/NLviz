"""F: in-process FastAPI authorization with REAL Jammi data, scripted model, local stores.

Not an HTTP test of Docker FastAPI or the production ALB. Identity overrides exercise the
real dependency access checks. No hosted/local model calls. Skips without JAMMI_TEST_URL.
JAMMI_TEST_UPLOAD_ROOT and JAMMI_TEST_UPLOAD_URL must name the SAME shared directory.
Grounding/examples have no public routes: callback access is covered through schema/chat;
embedding corpus isolation and RDS persistence are outside this file's boundary.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import uuid

import pytest

if not os.getenv('JAMMI_TEST_URL'):
    pytest.skip('JAMMI_TEST_URL not set', allow_module_level=True)

from app.api.routes.chat import router as chat_router
from app.api.routes.schema import router as schema_router
from app.api.routes.bench import router as bench_router
from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.dependencies import (get_connection_service, get_jammi_provisioner, get_jammi_upload_store,
                              get_job_manager, get_grounding)
from app.jammi.client import JammiClient, JammiSettings
from app.models import SemanticIntent
from app.models.connection import GenerationJob, SemanticSchemaVersion
from app.security.auth import AuthUser, get_current_user
from app.semantic.loader import SemanticRegistry
from app.semantic.models import SemanticSchema
from app.services.connection_service import ConnectionService, ConnectionRuntime
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter
from app.services.jammi_sources import JammiSourceProvisioner
from app.services.jammi_uploads import LocalUploadStore
from app.services.query_service import QueryService
from test_connections_route import _build_connection_app
from test_agent_conversation import FakeIndex, ScriptedLLM, _state_filter

pytestmark = pytest.mark.jammi


class Jobs:
    def __init__(self):
        self.jobs = {}
    def start_job(self, connection_id, connector):
        job = GenerationJob(job_id=str(uuid.uuid4()), connection_id=connection_id,
                            created_at=datetime.now(timezone.utc))
        self.jobs[job.job_id] = job
        return job
    def get_job(self, job_id):
        return self.jobs.get(job_id)


@pytest.fixture
def owned(tmp_path, monkeypatch):
    # Only credential-presence preflight is stubbed; generation itself is a job stub.
    monkeypatch.setattr('app.llm.missing_api_key', lambda provider: None)
    client, store, secrets = _build_connection_app(tmp_path)
    current = [AuthUser(user_id='owner-A')]
    client.app.dependency_overrides[get_current_user] = lambda: current[0]
    jammi = JammiClient(JammiSettings(os.environ['JAMMI_TEST_URL'], str(uuid.uuid4())))
    root = Path(os.environ.get('JAMMI_TEST_UPLOAD_ROOT', str(Path(__file__).resolve().parents[1] / 'data/parquet/_live_ownership')))
    uploads = LocalUploadStore(root, os.environ.get('JAMMI_TEST_UPLOAD_URL', '/data/_live_ownership'))
    service = ConnectionService(store, secrets, IntentMapperConfig(mode='heuristic'), jammi_client=jammi)
    jobs = Jobs()
    client.app.dependency_overrides[get_connection_service] = lambda: service
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: uploads
    client.app.dependency_overrides[get_job_manager] = lambda: jobs
    client.app.dependency_overrides[get_grounding] = lambda: None
    client.app.state.connection_store = store
    client.app.state.connection_service = service
    client.app.include_router(chat_router)
    client.app.include_router(schema_router)
    client.app.include_router(bench_router)
    response = client.post('/connections/upload', files={'file': ('sales.csv', b'id,state\n1,RJ\n2,RN\n3,SP\n', 'text/csv')},
                           data={'display_name': 'ownership synthetic fixture'})
    assert response.status_code == 200
    cid = response.json()['connection_id']
    registry = SemanticRegistry(SemanticSchema.model_validate({
        'version': '1', 'dataset': 'ownership', 'description': '',
        'tables': [{'name': 'sales'}], 'joins': [], 'time_dimensions': [],
        'metrics': [{'name': 'order_count', 'display_name': 'Orders', 'description': '',
                     'aggregation': 'COUNT_DISTINCT', 'sql_expression': 'sales.id', 'required_tables': ['sales']}],
        'dimensions': [{'name': 'customer_state', 'display_name': 'Customer State',
                        'sql_expression': 'sales.state', 'required_tables': ['sales']}]}))
    schema_path = tmp_path / 'ownership.yaml'
    schema_path.write_text(json.dumps(registry.schema.model_dump(mode='json', by_alias=True)), encoding='utf-8')
    for vid in ('v1', 'v2'):
        store.create_version(SemanticSchemaVersion(version_id=vid, connection_id=cid, status='validated',
                             created_at=datetime.now(timezone.utc), schema_path=str(schema_path)))
    assert client.post(f'/connections/{cid}/publish', json={'version_id': 'v1'}).status_code == 200
    yield client, store, service, jobs, current, cid, jammi, registry
    current[0] = AuthUser(user_id='owner-A')
    if store.get_connection(cid).status == 'active':
        assert client.delete(f'/connections/{cid}').status_code == 200


def _b(current):
    current[0] = AuthUser(user_id='user-B')


def _snapshot(store, cid, jammi):
    return (store.get_connection(cid).model_dump(mode='json'),
            [v.model_dump(mode='json') for v in store.list_versions(cid)], jammi.source_ids())


def test_list_and_schema_hide_other_owners(owned):
    client, store, service, jobs, user, cid, jammi, _ = owned
    assert any(p['connection_id'] == cid for p in client.get('/connections').json())
    headers = {'X-Connection-Id': cid}
    assert client.get('/schema', headers=headers).status_code == 200
    _b(user)
    assert all(p['connection_id'] != cid for p in client.get('/connections').json())
    assert client.get('/schema', headers=headers).status_code == 404


def test_files_denied_without_changing_sources_or_profile(owned):
    client, store, service, jobs, user, cid, jammi, _ = owned
    def attach(name):
        return client.post(f'/connections/{cid}/files', files={'file': (name, b'state,label\nRJ,Rio\n', 'text/csv')})
    assert attach('lookup.csv').status_code == 200
    before = _snapshot(store, cid, jammi)
    _b(user)
    assert attach('stolen.csv').status_code == 404
    assert _snapshot(store, cid, jammi) == before


def test_generation_and_jobs_are_owner_scoped(owned):
    client, store, service, jobs, user, cid, jammi, _ = owned
    response = client.post(f'/connections/{cid}/generate')
    assert response.status_code == 200
    job_id = response.json()['job_id']
    assert client.get(f'/connections/{cid}/jobs/{job_id}').status_code == 200
    before = _snapshot(store, cid, jammi), dict(jobs.jobs)
    _b(user)
    assert client.post(f'/connections/{cid}/generate').status_code == 404
    assert client.get(f'/connections/{cid}/jobs/{job_id}').status_code == 404
    assert (_snapshot(store, cid, jammi), jobs.jobs) == before


def test_publish_cannot_read_or_activate_other_owners_version(owned):
    client, store, service, jobs, user, cid, jammi, _ = owned
    assert store.get_published_version(cid).version_id == 'v1'  # owner-success publish in fixture
    before = _snapshot(store, cid, jammi)
    _b(user)
    assert client.post(f'/connections/{cid}/publish', json={'version_id': 'v2'}).status_code == 404
    assert _snapshot(store, cid, jammi) == before


@pytest.mark.parametrize('path', ['/chat', '/chat/stream'])
def test_chat_cannot_access_other_owners_runtime(owned, path):
    client, store, service, jobs, user, cid, jammi, _ = owned
    headers = {'X-Connection-Id': cid}
    body = {'question': 'count orders', 'intent': {'metric': 'order_count'}}
    owner = client.post(path, headers=headers, json=body)
    assert owner.status_code == 200
    if path.endswith('/stream'):
        assert 'event: result' in owner.text and 'event: error' not in owner.text
    else:
        assert owner.json()['status'] == 'answered'
    before = _snapshot(store, cid, jammi)
    _b(user)
    assert client.post(path, headers=headers, json=body).status_code == 404
    assert _snapshot(store, cid, jammi) == before


@pytest.mark.parametrize('resume_path', ['/chat/resume', '/chat/resume/stream'])
def test_shared_connection_conversation_is_scoped_and_denial_preserves_pause(owned, resume_path):
    client, store, service, jobs, user, cid, jammi, registry = owned
    runtime = service.get_runtime(cid)
    config = IntentMapperConfig(mode='llm', provider=None, model='scripted', answer_path='semantic')
    calls = []
    def index():
        calls.append('values')
        return FakeIndex()
    def examples():
        calls.append('examples')
        return None
    mapper = IntentMapperRouter(config=config, llm_mapper=ScriptedLLM(SemanticIntent(
        metric='order_count', filters=_state_filter(customer_state='Rio'))), value_index=index, example_index=examples)
    runner = QueryGraphRunner(QueryGraphDependencies(connector=runtime.connector,
        schema_context=runtime.schema_context, registry=registry, intent_mapper=mapper, intent_config=config,
        table_ref=runtime.connector.table_ref_resolver()))
    runtime.query_service = QueryService(runner)
    service.register_runtime('default', runtime)  # synthetic shared demo, to exercise thread scoping rather than connection denial
    for conversation in ('control', 'victim'):
        paused = client.post('/chat', json={'question': 'orders in Rio', 'conversation_id': conversation})
        assert paused.status_code == 200 and paused.json()['status'] == 'clarification'
        if conversation == 'control':
            control = client.post(resume_path, json={'conversation_id': conversation, 'answer': 'RJ'})
            assert control.status_code == 200
            assert ('event: result' in control.text and 'event: error' not in control.text) if resume_path.endswith('/stream') else control.json()['status'] == 'answered'
    before_calls = list(calls)
    _b(user)
    denied = client.post(resume_path, json={'conversation_id': 'victim', 'answer': 'RJ'})
    if resume_path.endswith('/stream'):
        assert 'NO_PENDING_CLARIFICATION' in denied.text and 'event: result' not in denied.text
    else:
        assert denied.status_code == 409
    assert calls == before_calls
    user[0] = AuthUser(user_id='owner-A')
    final = client.post('/chat/resume', json={'conversation_id': 'victim', 'answer': 'RJ'})
    assert final.status_code == 200 and final.json()['status'] == 'answered'


def test_delete_denied_preserves_connection_and_data(owned):
    client, store, service, jobs, user, cid, jammi, _ = owned
    # An owner-success delete on a second disposable connection is the positive control.
    other = client.post('/connections/upload', files={'file': ('delete_control.csv', b'id\n1\n', 'text/csv')},
                        data={'display_name': 'delete control'})
    assert other.status_code == 200
    assert client.delete('/connections/' + other.json()['connection_id']).status_code == 200
    before = _snapshot(store, cid, jammi)
    _b(user)
    assert client.delete(f'/connections/{cid}').status_code == 404
    assert _snapshot(store, cid, jammi) == before


@pytest.mark.parametrize('path,body', [
    ('/bench/sql', {'question': 'count orders'}),
    ('/bench/replay', {'turns': [{'question': 'count orders'}], 'phrase_rewrites': True}),
])
def test_development_bench_routes_check_owner_before_computation(owned, monkeypatch, path, body):
    client, store, service, jobs, user, cid, jammi, _ = owned
    calls = []
    def computed(*args, **kwargs):
        calls.append(path)
        return {'status': 'answered'} if path.endswith('/sql') else []
    monkeypatch.setattr('app.api.routes.bench.answer', computed)
    monkeypatch.setattr('app.api.routes.bench.replay_case', computed)
    headers = {'X-Connection-Id': cid}
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert calls == [path]
    _b(user)
    assert client.post(path, headers=headers, json=body).status_code == 404
    assert calls == [path]
