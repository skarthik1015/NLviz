from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.schema import router
from app.connectors.base import SchemaContext
from app.dependencies import get_connector, get_schema_context, get_semantic_registry
from app.security.auth import AuthUser, get_current_user
from app.semantic import load_semantic_registry


class StubConnector:
    def get_connector_type(self) -> str:
        return "stub"


def _load_registry():
    schema_path = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
    return load_semantic_registry(schema_path)


def test_schema_route_uses_semantic_join_definitions():
    registry = _load_registry()
    schema_context = SchemaContext(
        tables={"orders": [], "customers": []},
        row_counts={"orders": 1, "customers": 1},
        join_paths=[{"from_table": "fake", "to_table": "fake", "from_col": "id", "to_col": "id"}],
    )

    _test_user = AuthUser(user_id="test-user", email="test@example.com")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_connector] = lambda: StubConnector()
    app.dependency_overrides[get_schema_context] = lambda: schema_context
    app.dependency_overrides[get_semantic_registry] = lambda: registry
    app.dependency_overrides[get_current_user] = lambda: _test_user

    client = TestClient(app)
    response = client.get("/schema")

    assert response.status_code == 200
    payload = response.json()
    assert payload["join_paths"][0]["from_table"] == registry.schema.joins[0].from_table
    assert payload["join_paths"][0]["on"] == registry.schema.joins[0].on
    assert payload["join_paths"][0]["from_table"] != "fake"
