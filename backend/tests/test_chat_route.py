from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.chat import router
from app.dependencies import get_query_service
from app.models import ChatResponse, SemanticIntent
from app.security import SQLSafetyError
from app.security.auth import AuthUser, get_current_user


class SuccessService:
    def run_question(self, question: str, explicit_intent=None, debug: bool = False, **_conversation) -> ChatResponse:
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

    def run_question(self, question: str, explicit_intent=None, debug: bool = False, **_conversation):
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


# Conversations through the route, on the real agent graph ------------------------------------

from test_agent_conversation import FakeIndex, ScriptedLLM, _intent, _runner, _state_filter  # noqa: E402

from app.services.query_service import QueryService  # noqa: E402

_USER = {"current": _TEST_USER}


def _conversation_app():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="Rio")))
    runner, connector = _runner(llm, index=FakeIndex())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_query_service] = lambda: QueryService(runner)
    app.dependency_overrides[get_current_user] = lambda: _USER["current"]
    return TestClient(app), connector


def test_clarification_round_trip_through_the_api():
    client, connector = _conversation_app()

    paused = client.post("/chat", json={"question": "revenue in Rio", "conversation_id": "c1"}).json()
    assert paused["status"] == "clarification"
    assert paused["sql"] is None and paused["rows"] == []
    assert paused["clarification"]["options"] == ["RJ", "RN", "RS"]
    assert paused["conversation_id"] == "c1"

    done = client.post("/chat/resume", json={"conversation_id": "c1", "answer": "RJ"}).json()
    assert done["status"] == "answered"
    assert "'RJ'" in done["sql"] and done["row_count"] == 2
    assert any(e["kind"] == "clarification" for e in done["evidence"])

    again = client.post("/chat/resume", json={"conversation_id": "c1", "answer": "RJ"})
    assert again.status_code == 409 and again.json()["detail"] == "NO_PENDING_CLARIFICATION"


def test_one_off_question_gets_a_temporary_conversation_id_only_while_paused():
    client, _ = _conversation_app()

    paused = client.post("/chat", json={"question": "revenue in Rio"}).json()
    assert paused["conversation_id"].startswith("once-")

    done = client.post("/chat/resume", json={"conversation_id": paused["conversation_id"], "answer": "RS"}).json()
    assert done["conversation_id"] is None
    assert client.post("/chat/resume", json={"conversation_id": paused["conversation_id"], "answer": "RS"}).status_code == 409


def test_conversations_are_scoped_to_the_user_and_the_connection():
    client, _ = _conversation_app()
    client.post("/chat", json={"question": "revenue in Rio", "conversation_id": "shared"})

    _USER["current"] = AuthUser(user_id="someone-else")
    try:
        stolen = client.post("/chat/resume", json={"conversation_id": "shared", "answer": "RJ"})
    finally:
        _USER["current"] = _TEST_USER
    assert stolen.status_code == 409

    other_connection = client.post(
        "/chat/resume", json={"conversation_id": "shared", "answer": "RJ"},
        headers={"X-Connection-Id": "6f1c2b9e-3d4a-4f5b-8c7d-9e0a1b2c3d4e"},
    )
    assert other_connection.status_code == 409
    assert client.post("/chat/resume", json={"conversation_id": "shared", "answer": "RJ"}).status_code == 200


def test_conversation_ids_are_validated():
    client, _ = _conversation_app()
    assert client.post("/chat", json={"question": "x", "conversation_id": "../etc"}).status_code == 422
    assert client.post("/chat/resume", json={"conversation_id": "a/b", "answer": "RJ"}).status_code == 422


def _sse_events(response) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_stream_reports_each_step_then_the_result():
    client, _ = _conversation_app()

    response = client.post("/chat/stream", json={"question": "revenue in Rio", "conversation_id": "s1"})
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response)
    assert [name for name, _ in events[:-1]] == ["step"] * (len(events) - 1)
    assert [data["node"] for _, data in events[:-1]] == ["map_intent", "check_intent"]
    assert events[1][1]["evidence"][0]["kind"] == "match"
    assert all(isinstance(data["ms"], int) for _, data in events[:-1])
    kind, result = events[-1]
    assert kind == "result" and result["status"] == "clarification"

    resumed = _sse_events(client.post("/chat/resume/stream", json={"conversation_id": "s1", "answer": "RJ"}))
    nodes = [data["node"] for name, data in resumed if name == "step"]
    assert nodes == ["clarify", "check_intent", "sql_builder", "executor", "validator", "chart_selector", "explainer"]
    assert resumed[-1][0] == "result" and resumed[-1][1]["status"] == "answered" and resumed[-1][1]["row_count"] == 2


def test_stream_errors_are_events_without_internals():
    client, _ = _conversation_app()
    events = _sse_events(client.post("/chat/resume/stream", json={"conversation_id": "nothing", "answer": "RJ"}))
    assert events == [("error", {"status": 409, "detail": "NO_PENDING_CLARIFICATION"})]

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_query_service] = lambda: _Boom()
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    events = _sse_events(TestClient(app).post("/chat/stream", json={"question": "x"}))
    assert events == [("error", {"status": 500, "detail": "An internal error occurred. Please try again."})]


class _Boom:
    def stream_question(self, *args, **kwargs):
        raise RuntimeError("password=hunter2 at db:5432")


def test_a_database_that_is_down_is_a_503_not_an_internal_error():
    """Ops check: the dev Postgres stopped; Jammi answered after 30 s and the user saw a 500."""
    from app.api.routes.chat import _error

    jammi = RuntimeError("Flight returned internal error, with message: DataFusion error: Execution error: "
                         "PostgreSQL connection failed.\nTimed out in bb8\nAdjust the connection pool parameters")
    assert _error(jammi) == (503, "DATABASE_UNAVAILABLE")
    assert _error(RuntimeError("something else"))[0] == 500
