from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import pytest

from .budget import BudgetExceeded, SpendStore
from .health import health_details
from .middleware import QuestionLoggingMiddleware, ResponseObserver, capture_authenticated_user
from .pricing import PriceTable, is_local, money
from .service import Observability
from app.security.auth import AuthUser, get_current_user


def store(tmp_path, **kwargs):
    return SpendStore(tmp_path / "spend.sqlite3", **kwargs)


def test_standard_prices_are_decimal_and_configurable():
    prices = PriceTable()
    assert prices.get("openai", "gpt-4.1-mini").cost(1_000_000, 1_000_000) == Decimal("2.00")
    assert prices.get("openai", "gpt-4.1-mini").cost(1, 1) == Decimal("0.000002")
    override = PriceTable('{"openai/gpt-4.1-mini":{"input":"1.2","output":"3.4"}}')
    assert override.get("openai", "gpt-4.1-mini").cost(1_000_000, 1_000_000) == Decimal("4.6")
    assert prices.get("openai", "missing") is None


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "invalid"])
def test_invalid_money_rejected(value):
    with pytest.raises(ValueError):
        money(value)


def test_restart_idempotent_settlement_and_month_reset(tmp_path):
    now = [datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)]
    ledger = store(tmp_path, clock=lambda: now[0])
    reservation = ledger.reserve(Decimal("0.8"), Decimal("1"))
    restarted = store(tmp_path, clock=lambda: now[0])
    assert restarted.snapshot()["reserved_usd"] == "0.8"
    with pytest.raises(BudgetExceeded):
        restarted.reserve(Decimal("0.3"), Decimal("1"))
    now[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert restarted.snapshot()["spent_usd"] == "0"
    assert restarted.settle(reservation, Decimal("0.5"))
    assert not restarted.settle(reservation, Decimal("0.5"))
    # The late result belongs to October, not November.
    assert restarted.snapshot()["spent_usd"] == "0"
    now[0] = datetime(2026, 10, 31, tzinfo=timezone.utc)
    assert restarted.snapshot()["spent_usd"] == "0.5"
    assert restarted.snapshot()["reserved_usd"] == "0.0"


def test_atomic_admission_across_store_instances(tmp_path):
    ledgers = [store(tmp_path) for _ in range(12)]

    def admit(ledger):
        try:
            return ledger.reserve(Decimal("0.25"), Decimal("1"))
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as executor:
        admitted = list(executor.map(admit, ledgers))
    assert sum(reservation is not None for reservation in admitted) == 4
    assert ledgers[0].snapshot()["reserved_usd"] == "1.00"


def test_failed_unknown_usage_counts_and_blocks_hosted_not_local(tmp_path):
    service = Observability(store(tmp_path), cap="0.000002")
    call = service.before_call(provider="openai", model="gpt-4.1-mini",
                               prompt_tokens_upper_bound=1, max_completion_tokens=1)
    service.after_call(call, failed=True)
    assert service.store.snapshot()["spent_usd"] == "0.000002"
    with pytest.raises(BudgetExceeded):
        service.before_call(provider="openai", model="gpt-4.1-mini",
                            prompt_tokens_upper_bound=1, max_completion_tokens=1)
    local = service.before_call(provider="openai", model="qwen3:4b",
                                base_url="http://localhost:11434/v1",
                                prompt_tokens_upper_bound=100, max_completion_tokens=100)
    service.after_call(local, prompt_tokens=10, completion_tokens=10)
    assert service.store.snapshot()["spent_usd"] == "0.000002"


def test_unknown_hosted_price_fails_closed_and_zero_cap_blocks(tmp_path):
    service = Observability(store(tmp_path), cap="0")
    for model in ("missing", "gpt-4.1-mini"):
        with pytest.raises(BudgetExceeded):
            service.before_call(provider="openai", model=model,
                                prompt_tokens_upper_bound=0, max_completion_tokens=0)
    assert not is_local("openai", "https://custom.example/v1")
    assert not is_local("anthropic", "http://localhost")


def test_actual_cost_settles_reserve_and_counts_retry(tmp_path):
    service = Observability(store(tmp_path), cap="1")
    for failed in (True, False):
        call = service.before_call(provider="openai", model="gpt-4.1-mini",
                                   prompt_tokens_upper_bound=1000, max_completion_tokens=1000)
        service.after_call(call, prompt_tokens=100, completion_tokens=10, failed=failed)
        service.after_call(call, prompt_tokens=100, completion_tokens=10)  # idempotent
    assert Decimal(service.store.snapshot()["spent_usd"]) == Decimal("0.000112")
    assert Decimal(service.store.snapshot()["reserved_usd"]) == 0


def make_app(tmp_path, *, debug=False, audit=True):
    app = FastAPI()
    app.state.observability = Observability(store(tmp_path), debug_text=debug)
    app.state.config = SimpleNamespace(dev_user_id="user@example.test", trust_proxy_identity=False)
    app.state.database_pool = None
    app.state.jammi_provisioner = None
    app.add_middleware(QuestionLoggingMiddleware, api_prefix="/api")

    def result():
        data = {"query_id": "query-1", "status": "answered", "question": "SECRET_QUESTION",
                "rows": [{"secret": "SECRET_ROW"}], "sql": "SECRET_SQL"}
        if audit:
            data["audit"] = {"llm_calls": [{"provider": "openai", "model": "gpt-4.1-mini",
                                           "prompt_tokens": 100, "completion_tokens": 10}],
                             "steps": [{"secret": "SECRET_STEP"}]}
        return data

    @app.post("/api/chat", dependencies=[Depends(capture_authenticated_user)])
    async def chat(request: Request):
        await request.json()
        return result()

    @app.post("/api/chat/stream", dependencies=[Depends(capture_authenticated_user)])
    async def stream(request: Request):
        await request.json()

        async def body():
            yield 'event: step\ndata: {"text":"SECRET_STEP"}\n\n'
            yield "event: result\ndata: " + json.dumps(result()) + "\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    app.add_api_route("/health/details", health_details)
    return app


@pytest.fixture
def json_logs(caplog, monkeypatch):
    log = logging.getLogger("nlviz.questions")
    monkeypatch.setattr(log, "propagate", True)
    caplog.set_level(logging.INFO, logger="nlviz.questions")
    return caplog


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_json_sse_logs_redacted_and_response_unchanged(tmp_path, json_logs, path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(path, json={"question": "SECRET_QUESTION", "api_key": "SECRET_KEY"},
                               headers={"x-connection-id": "connection-1", "x-request-id": "request-1",
                                        "x-amzn-oidc-identity": "SPOOFED_USER"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-1"
    assert "SECRET_ROW" in response.text
    events = [json.loads(record.message) for record in json_logs.records if record.name == "nlviz.questions"]
    question = events[-1]
    assert question["query_id"] == "query-1"
    assert question["status"] == "answered"
    assert question["connection"] == "connection-1"
    assert question["prompt_tokens"] == 100
    assert Decimal(question["cost_usd"]) == Decimal("0.000056")
    assert question["user"] and "@" not in question["user"]
    for forbidden in ("SECRET_", "SPOOFED_USER", "user@example.test"):
        assert forbidden not in json.dumps(events)


def test_non_debug_usage_is_unknown_and_explicit_text_opt_in(tmp_path, json_logs):
    app = make_app(tmp_path, debug=True, audit=False)
    with TestClient(app) as client:
        client.post("/api/chat", json={"question": "allowed debug text", "password": "SECRET_PASSWORD"})
    event = json.loads([record.message for record in json_logs.records if record.name == "nlviz.questions"][-1])
    assert event["question"] == "allowed debug text"
    assert event["cost_usd"] is None and not event["usage_complete"]
    assert "SECRET_PASSWORD" not in json.dumps(event)


def test_sse_split_events_error_and_buffer_bound():
    observer = ResponseObserver()
    observer.sse = True
    for chunk in (b"event: res", b'ult\ndata: {"query_id":"q1",', b'"status":"unsupported"}\n\n'):
        observer.feed(chunk)
    assert observer.result["status"] == "unsupported"
    observer.feed(b'event: error\ndata: {"status":503,"detail":"SECRET"}\n\n')
    assert observer.result["status"] == "error"
    assert "SECRET" not in str(observer.result)
    observer.feed(b"x" * (1024 * 1024 + 1))
    assert observer.overflow and not observer.buffer


def test_health_requires_existing_auth_and_exposes_safe_configuration(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/health/details")
        assert response.status_code == 200
        data = response.json()
        assert data["app_database"] == "not_configured"
        assert data["jammi"] == "not_configured"
        assert data["spend"]["enforcement"] == "pending_llm_hooks"
        app.state.config.dev_user_id = None
        assert client.get("/health/details", headers={"x-amzn-oidc-identity": "spoof"}).status_code == 401


def test_health_checks_configured_dependencies_without_leaking_errors(tmp_path):
    app = make_app(tmp_path)

    class Pool:
        @contextmanager
        def acquire(self):
            raise RuntimeError("SECRET_DATABASE_DSN")
            yield

    class Jammi:
        def ping(self):
            return True

    app.state.database_pool = Pool()
    app.state.jammi_provisioner = SimpleNamespace(_client=Jammi())
    config = SimpleNamespace(provider="openai", model="gpt-4.1-mini")
    config.for_task = lambda task: config
    app.state.intent_config = config
    with TestClient(app) as client:
        response = client.get("/health/details")
    assert response.json()["app_database"] == "unreachable"
    assert response.json()["jammi"] == "reachable"
    assert response.json()["llm"]["tasks"]["schema"]["model"] == "gpt-4.1-mini"
    assert "SECRET" not in response.text


def test_http_failure_logs_error_without_error_text(tmp_path, json_logs):
    app = make_app(tmp_path)

    async def fail(request: Request):
        raise RuntimeError("SECRET_EXCEPTION")

    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) != "/api/chat"]
    app.add_api_route("/api/chat", fail, methods=["POST"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/chat", json={"question": "SECRET_QUESTION"})
    assert response.status_code == 500
    event = json.loads([record.message for record in json_logs.records if record.name == "nlviz.questions"][-1])
    assert event["status"] == "error"
    assert "SECRET" not in json.dumps(event)
