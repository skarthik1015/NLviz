"""Connection routes when Jammi is configured: Postgres connections become Jammi sources."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.api.routes.connections as routes_module
from app.connectors.postgres_connector import PostgresConnector
from app.dependencies import get_connection_service, get_jammi_provisioner
from app.models.connection import ConnectionProfile
from app.services.jammi_sources import JammiSourceProvisioner, source_id_for

from test_connections_route import _TEST_USER, _build_connection_app  # tests/ is on sys.path under pytest

PG_PARAMS = {"host": "db.internal", "port": 5432, "dbname": "sales", "user": "reader",
             "password": "s3cr3t/pw", "schema": "public"}


class FakeJammi:
    def __init__(self, *, fail_add_with: str | None = None):
        self.sources: dict[str, str] = {}
        self.removed: list[str] = []
        self.fail_add_with = fail_add_with

    def add_source(self, source_id, *, url, format=None):
        if self.fail_add_with:
            raise RuntimeError(self.fail_add_with)
        self.sources[source_id] = url

    def remove_source(self, source_id):
        self.removed.append(source_id)
        self.sources.pop(source_id, None)

    def describe(self, relation):
        return [{"column_name": "id", "data_type": "Int64", "is_nullable": "NO"}]

    def source_ids(self):
        return set(self.sources)


@pytest.fixture
def pg_tables(monkeypatch):
    tables = ["customers", "orders", "AuditLog"]
    monkeypatch.setattr(PostgresConnector, "list_tables", lambda self: list(tables))
    monkeypatch.setattr(routes_module, "_postgres_live_blockers", lambda params, tables: [])
    monkeypatch.setattr(routes_module, "_postgres_pre_epoch", lambda params, tables: [])
    return tables


def test_columns_with_dates_before_1970_are_left_out_and_the_database_stays_live(tmp_path: Path, pg_tables,
                                                                                 monkeypatch):
    """J36: rather than copying the database for one column, the column is left out and said so."""
    monkeypatch.setattr(routes_module, "_postgres_pre_epoch", lambda params, tables: ["customers.birth_date"])
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)
    body = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                              "display_name": "Sales"}).json()
    assert body["mode"] == "live" and "customers.birth_date" in body["notes"][0]
    assert secrets.get_by_connection_id(body["connection_id"])["hidden_columns"] == ["customers.birth_date"]


def _app_with_jammi(tmp_path: Path, jammi: FakeJammi):
    client, store, secrets = _build_connection_app(tmp_path)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    return client, store, secrets


def test_postgres_connection_is_registered_as_one_jammi_source(tmp_path: Path, pg_tables):
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)

    response = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                                  "display_name": "Sales"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped_tables"] == ["AuditLog"]  # mixed case: not addressable unquoted
    profile = store.get_connection(body["connection_id"])
    sid = source_id_for(_TEST_USER.user_id, body["connection_id"], "pg")
    assert profile.engine == "jammi"
    assert profile.workspace_id == _TEST_USER.user_id
    assert profile.jammi_tables == {"customers": f"{sid}.public.customers", "orders": f"{sid}.public.orders"}
    assert jammi.sources == {sid: "postgresql://reader:s3cr3t%2Fpw@db.internal:5432/sales"}
    assert secrets.get_by_connection_id(body["connection_id"]) == PG_PARAMS


def test_non_public_schema_is_refused_and_leaves_nothing_behind(tmp_path: Path, pg_tables):
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)

    response = client.post("/connections", json={"connector_type": "postgres",
                                                  "params": {**PG_PARAMS, "schema": "analytics"},
                                                  "display_name": "Analytics"})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("POSTGRES_SCHEMA_UNSUPPORTED")
    assert jammi.sources == {}
    assert store.list_connections(owner_id=_TEST_USER.user_id) == []
    assert not any((tmp_path / ".secrets").glob("*"))


def test_registration_failure_is_redacted_and_cleans_up(tmp_path: Path, pg_tables):
    jammi = FakeJammi(fail_add_with="InvalidArgument: cannot connect to postgresql://reader:s3cr3t%2Fpw@db.internal "
                                    "(password s3cr3t/pw rejected)")
    client, store, secrets = _app_with_jammi(tmp_path, jammi)

    response = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                                  "display_name": "Sales"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("JAMMI_REGISTRATION_FAILED")
    assert "s3cr3t" not in detail and "***" in detail
    assert store.list_connections(owner_id=_TEST_USER.user_id) == []
    assert not any((tmp_path / ".secrets").glob("*"))


def test_store_failure_deprovisions_the_new_sources(tmp_path: Path, pg_tables, monkeypatch):
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)

    def broken_create(profile):
        raise RuntimeError("database down")

    monkeypatch.setattr(store, "create_connection", broken_create)
    with pytest.raises(RuntimeError, match="database down"):
        client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                          "display_name": "Sales"})

    assert jammi.sources == {} and len(jammi.removed) == 1
    assert not any((tmp_path / ".secrets").glob("*"))


def test_delete_removes_the_connections_jammi_sources(tmp_path: Path):
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)
    connection_id = "123e4567-e89b-42d3-a456-556642440000"
    sid = source_id_for(_TEST_USER.user_id, connection_id, "pg")
    jammi.sources[sid] = "postgresql://..."
    store.create_connection(ConnectionProfile(
        connection_id=connection_id, display_name="Sales", connector_type="postgres",
        created_at=datetime.now(timezone.utc), owner_id=_TEST_USER.user_id, engine="jammi",
        workspace_id=_TEST_USER.user_id,
        jammi_tables={"orders": f"{sid}.public.orders", "customers": f"{sid}.public.customers"},
    ))
    secrets.put(connection_id, PG_PARAMS)

    class StubConnectionService:
        def evict_runtime(self, connection_id: str) -> None:
            pass

    client.app.dependency_overrides[get_connection_service] = lambda: StubConnectionService()
    response = client.delete(f"/connections/{connection_id}")

    assert response.status_code == 200
    assert jammi.removed == [sid] and jammi.sources == {}


def test_without_jammi_postgres_connections_are_refused(tmp_path: Path, pg_tables):
    """Phase 1g: Postgres is no longer queried directly; without Jammi the connection is refused."""
    client, store, secrets = _build_connection_app(tmp_path)  # no provisioner configured

    response = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                                  "display_name": "Sales"})

    assert response.status_code == 400 and response.json()["detail"].startswith("JAMMI_NOT_CONFIGURED")
    assert store.list_connections(owner_id=_TEST_USER.user_id) == []
    assert not any((tmp_path / ".secrets").glob("*"))


def test_database_discovery_failure_is_not_blamed_on_jammi(tmp_path: Path, monkeypatch):
    def refuse(self):
        raise RuntimeError('FATAL: password authentication failed for user "reader" (password s3cr3t/pw)')

    monkeypatch.setattr(PostgresConnector, "list_tables", refuse)
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)

    response = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                                  "display_name": "Sales"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == "AUTH_FAILED: wrong username or password" and "s3cr3t" not in detail
    assert jammi.sources == {} and not any((tmp_path / ".secrets").glob("*"))


def test_an_unreachable_database_is_reported_in_plain_words(tmp_path: Path, monkeypatch):
    """Ops check: creating returned the driver's text, Docker's internal address included."""
    def refuse(self):
        raise RuntimeError('connection to server at "host.docker.internal" (192.168.65.254), port 1 failed: '
                           'Connection refused')

    monkeypatch.setattr(PostgresConnector, "list_tables", refuse)
    client, _, _ = _app_with_jammi(tmp_path, FakeJammi())
    response = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS,
                                                  "display_name": "Sales"})
    assert response.status_code == 400
    assert response.json()["detail"] == "NETWORK_UNREACHABLE: cannot reach host — check host and port"


def test_legacy_connections_are_listed_but_not_served(tmp_path: Path):
    """Phase 1g: a connection created before Jammi still lists and deletes, but cannot be used."""
    from app.services.connection_service import ConnectionResolutionError, ConnectionService
    from app.services.intent_mapper import IntentMapperConfig

    client, store, secrets = _build_connection_app(tmp_path)
    connection_id = "123e4567-e89b-42d3-a456-556642440001"
    profile = ConnectionProfile(connection_id=connection_id, display_name="Old", connector_type="duckdb",
                                created_at=datetime.now(timezone.utc), owner_id=_TEST_USER.user_id)
    store.create_connection(profile)
    secrets.put(connection_id, {"db_path": str(tmp_path / "old.duckdb")})
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=FakeJammi())

    with pytest.raises(ConnectionResolutionError, match="recreate it"):
        service.build_connector(profile)
    from app.dependencies import get_job_manager

    client.app.dependency_overrides[get_connection_service] = lambda: service
    client.app.dependency_overrides[get_job_manager] = lambda: object()  # never reached
    response = client.post(f"/connections/{connection_id}/generate")
    assert response.status_code == 400 and response.json()["detail"].startswith("CONNECTION_UNAVAILABLE")
    assert [c.connection_id for c in store.list_connections(owner_id=_TEST_USER.user_id)] == [connection_id]
    assert client.delete(f"/connections/{connection_id}").status_code == 200


def test_production_requires_jammi(monkeypatch):
    from app.config import AppConfig
    from app.dependencies import build_all_services

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("JAMMI_URL", raising=False)
    with pytest.raises(RuntimeError, match="required in production"):
        build_all_services(config=AppConfig.from_env())


def test_a_postgres_database_jammi_cannot_read_live_is_copied_and_says_why(tmp_path: Path, monkeypatch):
    """One domain, jsonb, citext or timetz column fails a live Jammi source (J33); money reads 100x (J34)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.dependencies import get_jammi_upload_store
    from app.services.jammi_uploads import LocalUploadStore
    from app.services.mysql_snapshot import TableSnapshot

    monkeypatch.setattr(PostgresConnector, "list_tables", lambda self: ["film"])
    monkeypatch.setattr(routes_module, "_postgres_live_blockers",
                        lambda params, tables: ["film.release_year (domain year)"])
    monkeypatch.setattr(routes_module, "_postgres_foreign_keys", lambda params, tables: [])

    def fake_snapshot(connect, schema, tables, workdir, *, max_rows):
        path = workdir / "film.parquet"
        pq.write_table(pa.table({"film_id": [1]}), path)
        return [TableSnapshot("film", path, 1, {})]

    monkeypatch.setattr(routes_module, "snapshot_postgres_tables", fake_snapshot)
    jammi = FakeJammi()
    client, store, secrets = _app_with_jammi(tmp_path, jammi)
    uploads = LocalUploadStore(tmp_path / "uploads", "/uploads")
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: uploads

    response = client.post("/connections", json={"connector_type": "postgres", "params": PG_PARAMS, "display_name": "Films"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "snapshot" and "film.release_year (domain year)" in body["notes"][0]
    assert all(url.startswith("/uploads/") for url in jammi.sources.values())  # never the postgres:// URL
