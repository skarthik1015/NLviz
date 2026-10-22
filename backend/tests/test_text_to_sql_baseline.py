"""The text-to-SQL baseline (approach A of the ablation): context, table names, and its switch."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.services.text_to_sql import _metric_sql, build_context, to_physical
from tests.test_query_graph import StubConnector, _load_registry

REGISTRY = _load_registry()


def test_logical_tables_become_physical_and_keep_their_name_as_alias():
    sql = "SELECT o.order_status, COUNT(*) FROM orders o JOIN customers ON customers.customer_id = o.customer_id GROUP BY 1"
    physical = to_physical(sql, REGISTRY, lambda name: f"src_1.public.{name}")
    assert "src_1.public.orders AS o" in physical
    assert "src_1.public.customers AS customers" in physical


def test_ctes_and_unknown_names_are_left_alone():
    sql = "WITH orders_by_day AS (SELECT * FROM orders) SELECT * FROM orders_by_day"
    physical = to_physical(sql, REGISTRY, lambda name: f"s.public.{name}")
    assert "FROM orders_by_day" in physical and "s.public.orders AS orders" in physical
    assert to_physical(sql, REGISTRY, None) == sql


def test_the_context_has_every_metric_as_sql_and_the_hints_and_examples():
    metric = next(m for m in REGISTRY.schema.metrics if m.aggregation == "RATIO")
    assert "NULLIF" in _metric_sql(metric)
    examples = [{"question": "orders by state", "intent": {"metric": "order_count", "dimensions": ["customer_state"]}},
                {"question": "broken", "intent": {"metric": "no_such_metric"}}]
    context = json.loads(build_context(REGISTRY, StubConnector().get_schema(),
                                       [{"dimension": "customer_state", "value": "SP"}], examples))
    assert {m["name"] for m in context["metrics"]} == {m.name for m in REGISTRY.schema.metrics}
    from app.connectors import DuckDBConnector

    real = json.loads(build_context(REGISTRY, DuckDBConnector().get_schema(), None, None))
    assert "order_status VARCHAR" in real["tables"]["orders"]  # names and types, not null
    assert context["value_hints"] == [{"dimension": "customer_state", "value": "SP"}]
    assert [e["question"] for e in context["examples"]] == ["orders by state"]  # the broken one is left out
    assert "SELECT" in context["examples"][0]["sql"]


def test_the_bench_route_is_off_by_default(monkeypatch):
    monkeypatch.delenv("BENCH_ENDPOINTS", raising=False)
    from fastapi import FastAPI

    from app.api import build_api_router

    app = FastAPI()
    app.include_router(build_api_router())
    assert TestClient(app).post("/bench/sql", json={"question": "x"}).status_code == 404


def test_the_bench_route_refuses_production(monkeypatch):
    import pytest

    from app.api import build_api_router

    monkeypatch.setenv("BENCH_ENDPOINTS", "on")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        build_api_router()


def test_the_bench_route_reports_the_spending_cap_as_chat_does(monkeypatch):
    from fastapi import FastAPI

    from app.api import build_api_router
    from app.api.routes import bench

    def spent(*args, **kwargs):
        from app.observability.budget import BudgetExceeded

        raise BudgetExceeded("spent")

    monkeypatch.setenv("BENCH_ENDPOINTS", "on")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(bench, "answer", spent)
    monkeypatch.setattr(bench, "_get_runtime", lambda request, user: None)
    from app.security.auth import AuthUser, get_current_user

    app = FastAPI()
    app.include_router(build_api_router())
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="u")
    response = TestClient(app).post("/bench/sql", json={"question": "x"})
    assert (response.status_code, response.json()["detail"]) == (503, "LLM_BUDGET_EXCEEDED")
