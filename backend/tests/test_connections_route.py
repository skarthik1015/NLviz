from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.routes.connections import router as connections_router
from app.dependencies import (
    get_audit_log,
    get_connection_service,
    get_connection_store,
    get_job_manager,
    get_schema_storage,
    get_query_service,
    get_secret_store,
)
from app.models.connection import ConnectionProfile, GenerationJob, SemanticSchemaVersion
from app.security.auth import AuthUser, get_current_user
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionResolutionError
from app.services.connection_store import ConnectionStore
from app.services.secret_store import SecretStore
from app.storage.schema_storage import LocalSchemaStorage

_TEST_USER = AuthUser(user_id="test-user", email="test@example.com")


class MissingRuntimeService:
    def get_runtime(self, connection_id: str):
        raise ConnectionResolutionError(connection_id, "not found")


class NoPublishedSchemaService:
    def get_runtime(self, connection_id: str):
        raise ConnectionResolutionError(
            connection_id,
            "no published schema — generate and publish a schema first",
        )


class StubJobManager:
    def __init__(self, jobs: dict[str, GenerationJob] | None = None):
        self._jobs = jobs or {}

    def get_job(self, job_id: str):
        return self._jobs.get(job_id)


def _build_connection_app(tmp_path: Path) -> tuple[TestClient, ConnectionStore, SecretStore]:
    app = FastAPI()
    app.include_router(connections_router)

    connection_store = ConnectionStore(
        connections_path=tmp_path / "connections.jsonl",
        versions_path=tmp_path / "schema_versions.jsonl",
    )
    secret_store = SecretStore(secrets_dir=tmp_path / ".secrets")
    audit_log = AuditLog(path=tmp_path / "audit.jsonl")
    schema_storage = LocalSchemaStorage(base_dir=tmp_path / "schemas")

    app.dependency_overrides[get_connection_store] = lambda: connection_store
    app.dependency_overrides[get_secret_store] = lambda: secret_store
    app.dependency_overrides[get_audit_log] = lambda: audit_log
    app.dependency_overrides[get_schema_storage] = lambda: schema_storage
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER

    return TestClient(app), connection_store, secret_store


def test_invalid_connection_id_header_returns_400():
    app = FastAPI()

    @app.get("/probe")
    async def probe(service=Depends(get_query_service)):
        del service
        return {"ok": True}

    app.state.connection_service = MissingRuntimeService()
    client = TestClient(app)

    response = client.get("/probe", headers={"X-Connection-Id": "not-a-valid-id"})

    assert response.status_code == 400
    assert response.json()["detail"] == "INVALID_CONNECTION_ID"


def test_unknown_connection_id_returns_404():
    app = FastAPI()

    @app.get("/probe")
    async def probe(service=Depends(get_query_service)):
        del service
        return {"ok": True}

    app.state.connection_service = MissingRuntimeService()
    client = TestClient(app)

    response = client.get("/probe", headers={"X-Connection-Id": "123e4567-e89b-42d3-a456-556642440000"})

    assert response.status_code == 404
    assert response.json()["detail"] == "CONNECTION_NOT_FOUND"


def test_connection_without_published_schema_returns_409():
    app = FastAPI()

    @app.get("/probe")
    async def probe(service=Depends(get_query_service)):
        del service
        return {"ok": True}

    app.state.connection_service = NoPublishedSchemaService()
    client = TestClient(app)

    response = client.get("/probe", headers={"X-Connection-Id": "123e4567-e89b-42d3-a456-556642440000"})

    assert response.status_code == 409
    assert response.json()["detail"] == "SCHEMA_NOT_PUBLISHED"


def test_upload_creates_duckdb_with_sanitized_table_name(tmp_path: Path):
    client, store, secrets = _build_connection_app(tmp_path)
    files = {"file": ("2026 sales-data.csv", b"id,value\n1,10\n2,20\n", "text/csv")}
    data = {"display_name": "Uploaded CSV"}

    response = client.post("/connections/upload", files=files, data=data)

    assert response.status_code == 200
    payload = response.json()
    connection_id = payload["connection_id"]
    assert payload["status"] == "active"

    profile = store.get_connection(connection_id)
    assert profile is not None
    assert profile.connector_type == "duckdb"

    params = secrets.get_by_connection_id(connection_id)
    db_path = Path(params["db_path"])
    assert db_path.exists()

    with duckdb.connect(str(db_path), read_only=True) as conn:
        tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        # 2026 sales-data -> t_2026_sales_data
        assert "t_2026_sales_data" in tables
        row_count = conn.execute('SELECT COUNT(*) FROM "t_2026_sales_data"').fetchone()[0]
    assert row_count == 2


def test_upload_rejects_oversized_file(tmp_path: Path, monkeypatch):
    import app.api.routes.connections as connections_module

    monkeypatch.setattr(connections_module, "_MAX_UPLOAD_MB", 1)
    client, store, _ = _build_connection_app(tmp_path)
    big_payload = b"x" * (2 * 1024 * 1024)
    files = {"file": ("big.csv", big_payload, "text/csv")}
    data = {"display_name": "Too Big"}

    response = client.post("/connections/upload", files=files, data=data)

    assert response.status_code == 413
    assert "File exceeds 1MB limit" in response.json()["detail"]
    assert store.list_connections() == []


def test_list_connections_reports_query_readiness(tmp_path: Path):
    client, store, _ = _build_connection_app(tmp_path)

    connection_id = "123e4567-e89b-42d3-a456-556642440000"
    store.create_connection(
        profile=ConnectionProfile(
            connection_id=connection_id,
            display_name="Uploaded CSV",
            connector_type="duckdb",
            created_at=datetime.now(timezone.utc),
            owner_id=_TEST_USER.user_id,
        )
    )

    response = client.get("/connections")
    assert response.status_code == 200
    assert response.json()[0]["query_ready"] is False


def test_job_status_requires_connection_owner(tmp_path: Path):
    client, store, _ = _build_connection_app(tmp_path)
    connection_id = "123e4567-e89b-42d3-a456-556642440000"
    job_id = "job-1"
    store.create_connection(
        profile=ConnectionProfile(
            connection_id=connection_id,
            display_name="Private DB",
            connector_type="duckdb",
            created_at=datetime.now(timezone.utc),
            owner_id="other-user",
        )
    )
    client.app.dependency_overrides[get_job_manager] = lambda: StubJobManager(
        {
            job_id: GenerationJob(
                job_id=job_id,
                connection_id=connection_id,
                status="running",
                created_at=datetime.now(timezone.utc),
            )
        }
    )

    response = client.get(f"/connections/{connection_id}/jobs/{job_id}")

    assert response.status_code == 403
    assert response.json()["detail"] == "Access denied"


def test_test_connection_handles_boolean_failure_without_schema_introspection(tmp_path: Path, monkeypatch):
    client, _, _ = _build_connection_app(tmp_path)

    class FalseConnector:
        def test_connection(self) -> bool:
            return False

        def get_schema(self):
            raise AssertionError("schema introspection should not run after failed connection test")

        def close(self) -> None:
            return None

    monkeypatch.setattr("app.api.routes.connections._build_connector", lambda *_args, **_kwargs: FalseConnector())

    response = client.post(
        "/connections/test",
        json={"connector_type": "duckdb", "params": {"db_path": "missing.duckdb"}},
    )

    assert response.status_code == 200
    assert response.json() == {"success": False, "tables": [], "error": "CONNECTION_TEST_FAILED"}


def test_delete_connection_removes_uploaded_duckdb_and_schema_files(tmp_path: Path):
    client, store, secrets = _build_connection_app(tmp_path)
    connection_id = "123e4567-e89b-42d3-a456-556642440000"
    upload_path = tmp_path / "uploads" / f"{connection_id}.duckdb"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_text("db", encoding="utf-8")
    schema_path = tmp_path / "schemas" / connection_id / "version-1.yaml"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text("version: '1.0'\n", encoding="utf-8")

    store.create_connection(
        profile=ConnectionProfile(
            connection_id=connection_id,
            display_name="Uploaded CSV",
            connector_type="duckdb",
            created_at=datetime.now(timezone.utc),
            owner_id=_TEST_USER.user_id,
        )
    )
    store.create_version(
        SemanticSchemaVersion(
            version_id="version-1",
            connection_id=connection_id,
            status="published",
            created_at=datetime.now(timezone.utc),
            schema_path=str(schema_path),
        )
    )
    secrets.put(connection_id, {"db_path": str(upload_path)})

    class StubConnectionService:
        def evict_runtime(self, _connection_id: str) -> None:
            return None

    client.app.dependency_overrides[get_connection_service] = lambda: StubConnectionService()

    response = client.delete(f"/connections/{connection_id}")

    assert response.status_code == 200
    assert not upload_path.exists()
    assert not schema_path.exists()
