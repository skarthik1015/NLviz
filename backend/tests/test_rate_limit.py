from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.rate_limit import RateLimitMiddleware


def test_rate_limit_hits_429():
    current_time = 0.0

    def fake_clock() -> float:
        return current_time

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=2, clock=fake_clock)

    @app.post("/chat")
    async def chat():
        return {"ok": True}

    client = TestClient(app)

    first = client.post("/chat", json={"question": "one"})
    second = client.post("/chat", json={"question": "two"})
    third = client.post("/chat", json={"question": "three"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.headers["Retry-After"] == "30"
    assert "Retry in 30 seconds" in third.json()["detail"]


def test_rate_limit_uses_forwarded_for_header():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=1, clock=lambda: 0.0)

    @app.post("/chat")
    async def chat():
        return {"ok": True}

    client = TestClient(app)

    first = client.post("/chat", headers={"x-forwarded-for": "1.2.3.4"}, json={"question": "one"})
    second = client.post("/chat", headers={"x-forwarded-for": "5.6.7.8"}, json={"question": "two"})

    assert first.status_code == 200
    assert second.status_code == 200
