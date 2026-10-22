"""Per-user question limits (review B2: the old middleware matched only the path "/chat")."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.routes import chat as chat_routes
from app.dependencies import get_conversation_scope, get_query_service
from app.models import ChatResponse
from app.rate_limit import QueryLimiter
from app.security.auth import AuthUser, get_current_user


def test_the_rate_is_per_user_and_says_when_to_retry():
    now = [0.0]
    limiter = QueryLimiter(requests_per_minute=2, max_concurrent=0, clock=lambda: now[0])
    limiter.admit("a")()
    limiter.admit("a")()
    with pytest.raises(HTTPException) as exc:
        limiter.admit("a")
    assert exc.value.status_code == 429 and exc.value.headers["Retry-After"] == "30"
    limiter.admit("b")()  # another user has their own bucket
    now[0] = 30.0
    limiter.admit("a")()  # refilled


def test_questions_running_at_once_are_capped_until_released():
    limiter = QueryLimiter(requests_per_minute=0, max_concurrent=1)
    release = limiter.admit("a")
    with pytest.raises(HTTPException):
        limiter.admit("a")
    release()
    release()  # idempotent: a double release must not free a second slot
    second = limiter.admit("a")
    with pytest.raises(HTTPException):
        limiter.admit("a")
    second()


class _Service:
    def run_question(self, *args, **kwargs):
        return ChatResponse.model_construct()

    def stream_question(self, *args, **kwargs):
        yield {"event": "step", "node": "x"}


def _app(limiter: QueryLimiter) -> TestClient:
    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api")  # a prefix must not bypass the limit
    app.state.query_limiter = limiter
    app.dependency_overrides[get_query_service] = lambda: _Service()
    app.dependency_overrides[get_conversation_scope] = lambda: "u/c"
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="u")
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api/chat/stream", "/api/chat/resume/stream"])
def test_streaming_and_resume_routes_are_limited(path):
    client = _app(QueryLimiter(requests_per_minute=1, max_concurrent=0, clock=lambda: 0.0))
    body = {"question": "q"} if path.endswith("chat/stream") else {"conversation_id": "c1", "answer": "x"}
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json=body).status_code == 429


def test_a_finished_stream_frees_its_slot():
    limiter = QueryLimiter(requests_per_minute=0, max_concurrent=1)
    client = _app(limiter)
    for _ in range(3):  # each stream ends, so the single slot is free again
        response = client.post("/api/chat/stream", json={"question": "q"})
        assert response.status_code == 200 and "event: step" in response.text
