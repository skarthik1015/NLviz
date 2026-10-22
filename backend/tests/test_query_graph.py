from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors.base import DataConnector, SchemaContext
from app.models import SemanticIntent
from app.semantic import load_semantic_registry
from app.services import IntentMapperConfig, IntentMapperRouter, QueryService


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
        return pd.DataFrame(
            [
                {"customer_state": "SP", "metric_value": 12},
                {"customer_state": "RJ", "metric_value": 8},
            ]
        )

    def test_connection(self) -> bool:
        return True

    def get_connector_type(self) -> str:
        return "stub"


def _load_registry():
    schema_path = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
    return load_semantic_registry(schema_path)


class ExplodingMapper:
    def map(self, question, registry, schema):
        del question, registry, schema
        raise AssertionError("mapper should not run")


class StaticMapper:
    def __init__(self, intent: SemanticIntent):
        self.intent = intent

    def map(self, question, registry, schema):
        del question, registry, schema
        return self.intent


def test_query_graph_uses_explicit_intent_without_mapper():
    connector = StubConnector()
    schema_context = connector.get_schema()
    intent_config = IntentMapperConfig(mode="heuristic")
    runner = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=schema_context,
            registry=_load_registry(),
            intent_mapper=IntentMapperRouter(
                config=intent_config,
                heuristic_mapper=ExplodingMapper(),
            ),
            intent_config=intent_config,
        )
    )

    explicit_intent = SemanticIntent(metric="order_count", dimensions=["customer_state"], limit=10)
    state = runner.invoke(
        question="ignored because explicit intent is provided",
        query_id="query-1",
        explicit_intent=explicit_intent,
    )

    assert state["intent"] == explicit_intent
    assert state["intent_source"] == "explicit"
    assert state["row_count"] == 2
    assert "COUNT(DISTINCT orders.order_id) AS metric_value" in state["sql"]
    assert connector.executed_sql == [state["sql"]]
    assert state["user_trace"][0].startswith("Understood:")


def test_query_service_runs_langgraph_pipeline_end_to_end():
    connector = StubConnector()
    schema_context = connector.get_schema()
    intent_config = IntentMapperConfig(mode="heuristic")
    runner = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=schema_context,
            registry=_load_registry(),
            intent_mapper=IntentMapperRouter(
                config=intent_config,
                heuristic_mapper=StaticMapper(
                    SemanticIntent(
                        metric="total_revenue",
                        dimensions=["customer_state"],
                        time_dimension="order_date",
                        time_granularity="month",
                        order_by="time_asc",
                        limit=25,
                    )
                ),
            ),
            intent_config=intent_config,
        )
    )
    service = QueryService(query_graph=runner)

    response = service.run_question("Show revenue by customer state over time")

    assert response.intent.metric == "total_revenue"
    assert response.intent_source == "heuristic"
    assert response.row_count == 2
    assert response.rows[0]["customer_state"] == "SP"
    assert "DATE_TRUNC('month', orders.order_purchase_timestamp) AS time_bucket" in response.sql
    assert "SUM(op.payment_value) AS metric_value" in response.sql
    assert response.trace[0].startswith("Understood:")
    assert "SQL compiled from semantic model" in response.trace
    assert "Query executed: 2 row(s) returned" in response.trace
    assert "Validated: 2 rows" in response.trace
    assert "Summary generated" in response.trace


def test_executor_turns_missing_values_into_null():
    """A NULL time bucket arrives from pandas as NaT, which JSON cannot encode (was a 500)."""
    import json
    import math

    from app.agent.nodes.executor import _sanitize_rows

    frame = pd.DataFrame({"time_bucket": pd.to_datetime(["2018-01-01", None]), "metric_value": [1.5, math.nan]})
    rows = _sanitize_rows(frame.to_dict(orient="records"))
    assert rows[1] == {"time_bucket": None, "metric_value": None}
    assert rows[0]["metric_value"] == 1.5
    json.dumps(rows, default=str)


def test_a_total_over_no_matching_rows_is_an_empty_answer_not_a_crash():
    """One NULL row ("market cap of Apple" filtered on name = 'Apple'; the file says "Apple Inc.")
    reached the stat chart's float(None) and the question failed with a 500 (gpt-4.1-mini, run 7)."""
    from app.agent.nodes.chart_selector import build_chart_selector_node
    from app.agent.nodes.validator import build_validator_node
    from app.models.semantic_intent import SemanticIntent

    state = {"intent": SemanticIntent(metric="m"), "rows": [{"metric_value": None}], "row_count": 1}
    assert build_validator_node()(state)["validation_status"] == "empty"
    assert build_chart_selector_node()(state)["chart_spec"] is None
