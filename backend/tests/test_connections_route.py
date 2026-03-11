from __future__ import annotations

from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.routes.connections import router as connections_router
from app.dependencies import (
    get_audit_log,
    get_connection_store,
    get_query_service,
    get_secret_store,
)
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionResolutionError
from app.services.connection_store import ConnectionStore
from app.services.secret_store import SecretStore


class MissingRuntimeService:
    def get_runtime(self, connection_id: str):
        raise ConnectionResolutionError(connection_id, "not found")


def _build_connection_app(tmp_path: Path) -> tuple[TestClient, ConnectionStore, SecretStore]:
    app = FastAPI()
    app.include_router(connections_router)

    connection_store = ConnectionStore(
        connections_path=tmp_path / "connections.jsonl",
        versions_path=tmp_path / "schema_versions.jsonl",
    )
    secret_store = SecretStore(secrets_dir=tmp_path / ".secrets")
    audit_log = AuditLog(path=tmp_path / "audit.jsonl")

    app.dependency_overrides[get_connection_store] = lambda: connection_store
    app.dependency_overrides[get_secret_store] = lambda: secret_store
    app.dependency_overrides[get_audit_log] = lambda: audit_log

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
