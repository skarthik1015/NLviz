from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

import pandas as pd
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors.base import DataConnector, SchemaContext
from app.services import HeuristicIntentMapper, IntentMapperConfig, IntentMapperRouter, QueryService
from app.semantic import load_semantic_registry

_ACCURACY_THRESHOLD = 0.80


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
        dimensions=("customer_state", "customer_id"),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("c.customer_state AS customer_state", "c.customer_id AS customer_id"),
    ),
    GoldenCase(
        question="Show revenue by state",
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
        dimensions=("product_category",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("AVG(r.review_score) AS metric_value", "GROUP BY p.product_category_name_english"),
    ),
    GoldenCase(
        question="Show delivery time by seller state",
        metric="average_delivery_days",
        dimensions=("seller_state",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("s.seller_state AS seller_state", "AVG(DATEDIFF('day'"),
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
    GoldenCase(
        question="weekly revenue trend",
        metric="total_revenue",
        dimensions=(),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("DATE_TRUNC('week', orders.order_purchase_timestamp) AS time_bucket", "ORDER BY time_bucket ASC"),
    ),
    GoldenCase(
        question="quarterly revenue trend",
        metric="total_revenue",
        dimensions=(),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("DATE_TRUNC('quarter', orders.order_purchase_timestamp) AS time_bucket", "ORDER BY time_bucket ASC"),
    ),
    GoldenCase(
        question="yearly revenue trend",
        metric="total_revenue",
        dimensions=(),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("DATE_TRUNC('year', orders.order_purchase_timestamp) AS time_bucket", "ORDER BY time_bucket ASC"),
    ),
    GoldenCase(
        question="daily orders trend",
        metric="order_count",
        dimensions=(),
        time_dimension="order_date",
        order_by="time_asc",
        sql_contains=("DATE_TRUNC('day', orders.order_purchase_timestamp) AS time_bucket", "ORDER BY time_bucket ASC"),
    ),
    GoldenCase(
        question="top 10 customers by revenue",
        metric="total_revenue",
        dimensions=("customer_id",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("c.customer_id AS customer_id", "ORDER BY metric_value DESC", "LIMIT 10"),
    ),
    GoldenCase(
        question="bottom 7 customers by revenue",
        metric="total_revenue",
        dimensions=("customer_id",),
        time_dimension=None,
        order_by="metric_asc",
        sql_contains=("c.customer_id AS customer_id", "ORDER BY metric_value ASC", "LIMIT 7"),
    ),
    GoldenCase(
        question="first 3 customers by revenue",
        metric="total_revenue",
        dimensions=("customer_id",),
        time_dimension=None,
        order_by="metric_asc",
        sql_contains=("c.customer_id AS customer_id", "ORDER BY metric_value ASC", "LIMIT 3"),
    ),
    GoldenCase(
        question="Revenue in 2018 by product category",
        metric="total_revenue",
        dimensions=("product_category",),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("p.product_category_name_english AS product_category", ">= '2018-01-01'", "<= '2018-12-31'"),
    ),
    GoldenCase(
        question="How many orders were placed in 2017",
        metric="order_count",
        dimensions=(),
        time_dimension=None,
        order_by="metric_desc",
        sql_contains=("COUNT(DISTINCT orders.order_id) AS metric_value", ">= '2017-01-01'", "<= '2017-12-31'"),
    ),
)


class StubConnector(DataConnector):
    def __init__(self):
        self.executed_sql: list[str] = []

    def get_schema(self) -> SchemaContext:
        table_names = {
            "orders",
            "order_items",
            "order_payments",
            "order_reviews",
            "customers",
            "sellers",
            "products",
        }
        return SchemaContext(
            tables={table_name: [] for table_name in table_names},
            row_counts={},
            join_paths=[],
        )

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
    schema_context = connector.get_schema()
    intent_config = IntentMapperConfig(mode="heuristic")
    runner = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=schema_context,
            registry=_load_registry(),
            intent_mapper=IntentMapperRouter(
                config=intent_config,
                heuristic_mapper=HeuristicIntentMapper(),
            ),
            intent_config=intent_config,
        )
    )
    return QueryService(query_graph=runner)


def _evaluate_case(case: GoldenCase) -> tuple[bool, str]:
    connector = StubConnector()
    service = _build_service(connector)
    response = service.run_question(case.question)
    checks = [
        response.question == case.question,
        response.intent.metric == case.metric,
        tuple(response.intent.dimensions) == case.dimensions,
        response.intent.time_dimension == case.time_dimension,
        response.intent.order_by == case.order_by,
        response.intent_source == "heuristic",
        response.row_count == 1,
        response.trace[0].startswith("Understood:"),
        "SQL compiled from semantic model" in response.trace,
        any(msg.startswith("Query executed:") for msg in response.trace),
        connector.executed_sql == [response.sql],
        all(fragment in response.sql for fragment in case.sql_contains),
    ]
    if all(checks):
        return True, "pass"
    return False, f"failed for question={case.question!r}"


def _compute_accuracy() -> tuple[int, int, float]:
    passed = 0
    for case in GOLDEN_CASES:
        ok, _ = _evaluate_case(case)
        if ok:
            passed += 1
    total = len(GOLDEN_CASES)
    return passed, total, (passed / total if total else 0.0)


def test_golden_suite_size():
    assert len(GOLDEN_CASES) == 20


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.question)
def test_golden_question_outputs(case: GoldenCase):
    ok, detail = _evaluate_case(case)
    assert ok, detail


def test_golden_accuracy_gate():
    passed, total, accuracy = _compute_accuracy()
    assert accuracy >= _ACCURACY_THRESHOLD, (
        f"Golden accuracy gate failed: {passed}/{total} ({accuracy:.1%}) < {_ACCURACY_THRESHOLD:.0%}"
    )


if __name__ == "__main__":
    passed, total, accuracy = _compute_accuracy()
    print(f"Golden accuracy: {passed}/{total} ({accuracy:.1%})")
    if accuracy < _ACCURACY_THRESHOLD:
        raise SystemExit(1)
    raise SystemExit(pytest.main([__file__]))
