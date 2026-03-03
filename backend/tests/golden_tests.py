from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors.base import DataConnector, SchemaContext
from app.services import QueryService
from app.services.intent_mapper import map_question_to_intent
from app.semantic import load_semantic_registry


@dataclass(frozen=True)
class GoldenCase:
    question: str
    metric: str
    dimensions: tuple[str, ...]
    time_dimension: str | None
    order_by: str
    sql_contains: tuple[str, ...]


GOLDEN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        question="Show revenue",
        metric="total_revenue",
        dimensions=(),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("SUM(op.payment_value) AS metric_value", "LEFT JOIN order_payments op"),
    ),
    GoldenCase(
        question="Show revenue by customer state",
        metric="total_revenue",
        dimensions=("customer_state",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("c.customer_state AS customer_state", "LEFT JOIN customers c"),
    ),
    GoldenCase(
        question="Show orders by product category",
        metric="order_count",
        dimensions=("product_category",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("p.product_category_name_english AS product_category", "COUNT(DISTINCT orders.order_id)"),
    ),
    GoldenCase(
        question="Show average order value by payment method",
        metric="average_order_value",
        dimensions=("payment_type",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("AVG(op.payment_value) AS metric_value", "op.payment_type AS payment_type"),
    ),
    GoldenCase(
        question="Show review score by product category",
        metric="average_review_score",
        dimensions=("product_category", "review_score"),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("AVG(r.review_score) AS metric_value", "r.review_score AS review_score"),
    ),
    GoldenCase(
        question="Show delivery time by seller state",
        metric="average_delivery_days",
        dimensions=("seller_state",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("AVG(DATEDIFF('day', orders.order_purchase_timestamp, orders.order_delivered_customer_date))", "s.seller_state AS seller_state"),
    ),
    GoldenCase(
        question="Show cancellation rate by order status",
        metric="cancellation_rate",
        dimensions=("order_status",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("orders.order_status AS order_status", "::DOUBLE / NULLIF"),
    ),
    GoldenCase(
        question="Show revenue over time",
        metric="total_revenue",
        dimensions=(),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("DATE_TRUNC('month', orders.order_purchase_timestamp) AS time_bucket", "ORDER BY time_bucket ASC"),
    ),
    GoldenCase(
        question="Show orders by month",
        metric="order_count",
        dimensions=(),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("DATE_TRUNC('month', orders.order_purchase_timestamp) AS time_bucket", "LIMIT 100"),
    ),
    GoldenCase(
        question="Show seller state orders over time",
        metric="order_count",
        dimensions=("seller_state",),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("s.seller_state AS seller_state", "ORDER BY time_bucket ASC"),
    ),
)


class StubConnector(DataConnector):
    def __init__(self):
        self.executed_sql: list[str] = []

    def get_schema(self) -> SchemaContext:
        return SchemaContext(tables={}, row_counts={}, join_paths=[])

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        self.executed_sql.append(sql)
        return pd.DataFrame([{"metric_value": 1}])

    def test_connection(self) -> bool:
        return True

    def get_connector_type(self) -> str:
        return "stub"


def _load_registry():
    schema_path = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
    return load_semantic_registry(schema_path)


def _build_service(connector: StubConnector) -> QueryService:
    runner = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            registry=_load_registry(),
            intent_mapper=map_question_to_intent,
        )
    )
    return QueryService(query_graph=runner)


def test_golden_suite_size():
    assert len(GOLDEN_CASES) == 10


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.question)
def test_golden_question_outputs(case: GoldenCase):
    connector = StubConnector()
    service = _build_service(connector)

    response = service.run_question(case.question)

    assert response.question == case.question
    assert response.intent.metric == case.metric
    assert tuple(response.intent.dimensions) == case.dimensions
    assert response.intent.time_dimension == case.time_dimension
    assert response.intent.order_by == case.order_by
    assert response.row_count == 1
    assert response.trace[0].startswith("Intent mapper:")
    assert response.trace[-1].startswith("Executor:")
    for expected_fragment in case.sql_contains:
        assert expected_fragment in response.sql
    assert connector.executed_sql == [response.sql]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
