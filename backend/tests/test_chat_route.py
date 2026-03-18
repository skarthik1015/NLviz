from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.chat import router
from app.dependencies import get_query_service
from app.models import ChatResponse, SemanticIntent
from app.security import SQLSafetyError
from app.security.auth import AuthUser, get_current_user


class SuccessService:
    def run_question(self, question: str, explicit_intent=None, debug: bool = False) -> ChatResponse:
        del explicit_intent, debug
        intent = SemanticIntent(metric="order_count", limit=10)
        return ChatResponse(
            query_id="q-1",
            question=question,
            intent=intent,
            intent_source="heuristic",
            sql="SELECT COUNT(*) AS metric_value FROM orders LIMIT 10",
            rows=[{"metric_value": 1}],
            row_count=1,
            trace=["Understood: order count"],
        )


class InvalidService:
    def __init__(self, error: Exception):
        self._error = error

    def run_question(self, question: str, explicit_intent=None, debug: bool = False):
        del question, explicit_intent, debug
        raise self._error


_TEST_USER = AuthUser(user_id="test-user", email="test@example.com")


def _build_app(service) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_query_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    return app


def test_chat_route_success():
    client = TestClient(_build_app(SuccessService()))
    response = client.post("/chat", json={"question": "How many orders?"})
    assert response.status_code == 200
    assert response.json()["query_id"] == "q-1"


def test_chat_route_maps_invalid_value_error_to_stable_400():
    client = TestClient(_build_app(InvalidService(ValueError("bad input"))))
    response = client.post("/chat", json={"question": "bad"})
    assert response.status_code == 400
    assert response.json()["detail"] == "INVALID_OR_UNSAFE_QUERY"


def test_chat_route_maps_sql_safety_error_to_stable_400():
    client = TestClient(_build_app(InvalidService(SQLSafetyError("blocked"))))
    response = client.post("/chat", json={"question": "drop table"})
    assert response.status_code == 400
    assert response.json()["detail"] == "INVALID_OR_UNSAFE_QUERY"
