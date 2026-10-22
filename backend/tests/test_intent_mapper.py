from __future__ import annotations

from pathlib import Path

import pytest

from app.connectors.base import SchemaContext
from app.models import SemanticIntent
from app.semantic import load_semantic_registry
from app.services import (
    HeuristicIntentMapper,
    IntentMapperConfig,
    IntentMapperRouter,
    IntentMappingError,
    IntentValidationError,
    validate_semantic_intent,
)


def _load_registry():
    schema_path = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
    return load_semantic_registry(schema_path)


def _schema_context() -> SchemaContext:
    return SchemaContext(
        tables={
            "orders": [],
            "order_items": [],
            "order_payments": [],
            "order_reviews": [],
            "customers": [],
            "sellers": [],
            "products": [],
        },
        row_counts={},
        join_paths=[],
    )


class StaticMapper:
    def __init__(self, intent: SemanticIntent):
        self.intent = intent

    def map(self, question, registry, schema):
        del question, registry, schema
        return self.intent


class FailingMapper:
    def __init__(self, error: Exception):
        self.error = error

    def map(self, question, registry, schema):
        del question, registry, schema
        raise self.error


def test_heuristic_bottom_n_maps_to_metric_asc():
    intent = HeuristicIntentMapper().map(
        "bottom 10 customers by revenue",
        registry=_load_registry(),
        schema=_schema_context(),
    )

    assert intent.order_by == "metric_asc"
    assert intent.limit == 10
    assert intent.dimensions == ["customer_id"]


def test_heuristic_weekly_trend_uses_week_grain():
    intent = HeuristicIntentMapper().map(
        "weekly revenue trend",
        registry=_load_registry(),
        schema=_schema_context(),
    )

    assert intent.time_dimension == "order_date"
    assert intent.time_granularity == "week"
    assert intent.order_by == "time_asc"


def test_heuristic_rejects_question_with_no_matching_metric():
    with pytest.raises(IntentMappingError, match="no matching metric"):
        HeuristicIntentMapper().map(
            "median employee tenure by department",
            registry=_load_registry(),
            schema=SchemaContext(
                tables={"customers": [], "orders": []},
                row_counts={},
                join_paths=[],
            ),
        )


def test_heuristic_rejects_ambiguous_grouping_tail_without_schema_match():
    router = IntentMapperRouter(
        config=IntentMapperConfig(mode="heuristic"),
        heuristic_mapper=HeuristicIntentMapper(),
    )

    schema = SchemaContext(tables={"customers": [], "orders": [], "order_payments": []}, row_counts={}, join_paths=[])
    proposal = router.propose("show revenue by seller state", _load_registry(), schema)
    with pytest.raises(IntentValidationError, match="unknown tables"):
        validate_semantic_intent(proposal.intent, _load_registry(), schema)


def test_validate_semantic_intent_rejects_unknown_metric():
    with pytest.raises(IntentValidationError, match="Unknown metric"):
        validate_semantic_intent(
            SemanticIntent(metric="not_a_metric", limit=10),
            registry=_load_registry(),
            schema=_schema_context(),
        )


def test_llm_router_uses_llm_source_when_valid():
    router = IntentMapperRouter(
        config=IntentMapperConfig(mode="llm", provider="openai", model="test-model"),
        llm_mapper=StaticMapper(
            SemanticIntent(
                metric="total_revenue",
                dimensions=["customer_state"],
                limit=25,
            )
        ),
    )

    result = router.propose("show revenue by state", _load_registry(), _schema_context())

    assert result.source == "llm" and result.error is None
    assert result.intent.metric == "total_revenue"
    assert any(line.startswith("Intent mapper: source=llm") for line in result.trace)


def test_llm_router_falls_back_on_invalid_intent_with_trace():
    router = IntentMapperRouter(
        config=IntentMapperConfig(
            mode="llm",
            provider="openai",
            model="test-model",
            fallback_to_heuristic=True,
        ),
        llm_mapper=StaticMapper(
            SemanticIntent(
                metric="not_a_metric",
                limit=10,
            )
        ),
    )

    proposal = router.propose("show orders by customer state", _load_registry(), _schema_context())
    with pytest.raises(IntentValidationError):  # the graph rejects it, repairs, then falls back
        validate_semantic_intent(proposal.intent, _load_registry(), _schema_context())
    result = router.fallback("show orders by customer state", _load_registry(), _schema_context(), "Unknown metric")

    assert result.source == "llm_fallback"
    assert result.intent.metric == "order_count"
    assert result.trace[0].startswith("Intent mapper: source=llm_fallback reason=")


def test_llm_router_raises_without_fallback_on_invalid_intent():
    router = IntentMapperRouter(
        config=IntentMapperConfig(
            mode="llm",
            provider="openai",
            model="test-model",
            fallback_to_heuristic=False,
        ),
        llm_mapper=FailingMapper(IntentMappingError("bad llm response")),
    )

    assert router.propose("show orders by state", _load_registry(), _schema_context()).error == "bad llm response"
    with pytest.raises(IntentMappingError, match="LLM intent mapping failed"):
        router.fallback("show orders by state", _load_registry(), _schema_context(), "bad llm response")


def test_json_fallback_unwraps_an_intent_written_in_the_examples_shape():
    """Examples are shown as {"question", "intent"}; a small model answered in that shape and the
    correct intent was rejected (Phase 3d)."""
    from app.services.intent_mapper import _extract_intent_from_json_client

    class Wrapped:
        def complete_json(self, **_):
            return '{"intent": {"metric": "cancellation_rate", "dimensions": ["order_status"]}}'

    intent = _extract_intent_from_json_client(client=Wrapped(), system_prompt="", user_prompt="", model="m",
                                              timeout_ms=1000)
    assert intent.metric == "cancellation_rate" and intent.dimensions == ["order_status"]


def test_validation_rejects_breakdowns_no_chart_can_show():
    import pytest

    from app.services.intent_mapper import IntentValidationError, validate_semantic_intent

    registry, schema = _load_registry(), _schema_context()
    validate_semantic_intent(SemanticIntent(metric="total_revenue", dimensions=["customer_state", "payment_type"]),
                             registry, schema)
    validate_semantic_intent(SemanticIntent(metric="total_revenue", dimensions=["payment_type"],
                                            time_dimension="order_date", time_granularity="month"), registry, schema)
    with pytest.raises(IntentValidationError, match="At most 2"):
        validate_semantic_intent(SemanticIntent(metric="total_revenue",
                                                dimensions=["customer_state", "payment_type", "seller_state"]),
                                 registry, schema)
    with pytest.raises(IntentValidationError, match="At most 1"):
        validate_semantic_intent(SemanticIntent(metric="total_revenue", dimensions=["customer_state", "payment_type"],
                                                time_dimension="order_date", time_granularity="month"),
                                 registry, schema)


def test_answerability_check_reads_the_verdict_and_never_blocks_on_failure():
    from app.services.intent_mapper import check_answerable

    def client(raw):
        return type("Raw", (), {"complete_json": lambda self, **_: raw})()

    registry = _load_registry()
    assert check_answerable("q", registry, client('{"answerable": false, "missing": "product weight"}'), "m", 1)         == "product weight"
    assert check_answerable("q", registry, client('{"answerable": true}'), "m", 1) is None
    assert check_answerable("q", registry, client("not json"), "m", 1) is None
    assert check_answerable("q", registry, client('{"answerable": false}'), "m", 1) == "what this question needs"



def test_answerability_sees_columns_and_time_grains():
    # The benchmark's TPC-H layer: "n_name (N Name)" and no word on grains; "revenue by year" and
    # "by nation" were refused as missing.
    from app.services.intent_mapper import check_answerable

    seen = {}

    class Client:
        def complete_json(self, *, system_prompt, user_prompt, **_):
            seen.update(system=system_prompt, user=user_prompt)
            return '{"answerable": true}'

    registry = _load_registry()
    check_answerable("Revenue by year", registry, Client(), "m", 1)
    assert "grouped by day, week, month, quarter or year" in seen["system"]
    dimension = registry.schema.dimensions[0]
    assert dimension.sql_expression[:40] in seen["user"] and '"time_grains"' in seen["user"]


def test_answerability_veto_when_the_missing_thing_is_in_the_model():
    from app.services.intent_mapper import _names_registry_item, check_answerable

    registry = _load_registry()
    assert _names_registry_item("order_status", registry)
    assert _names_registry_item("seller_state average_delivery_days", registry)
    assert _names_registry_item("the Payment Method", registry)
    assert not _names_registry_item("product weight", registry)  # shares only a word with Product Category
    assert not _names_registry_item("customer city", registry)
    client = type("Raw", (), {"complete_json": lambda self, **_: '{"answerable": false, "missing": "payment_type"}'})()
    assert check_answerable("orders paid with a coupon", registry, client, "m", 1) is None


def test_heuristic_keeps_at_most_two_breakdowns_the_question_names():
    """After the LLM failed on Zomato, the keyword heuristic matched 3-4 dimensions and the
    question failed as an "unsafe query" (run 9). It keeps the named ones, up to the cap."""
    router = IntentMapperRouter(config=IntentMapperConfig(mode="heuristic"), heuristic_mapper=HeuristicIntentMapper())
    result = router.fallback("revenue by customer state, order status and product category and payment type",
                             _load_registry(), _schema_context(), "the LLM failed")
    assert len(result.intent.dimensions) == 2
    assert set(result.intent.dimensions) <= {"customer_state", "order_status", "product_category", "payment_type"}


_TWO_DATES = """
version: "1"
dataset: hr
description: test
tables: [{name: employees}, {name: salaries}]
joins: [{from: salaries, to: employees, "on": "salaries.emp_no = employees.emp_no", relationship: many_to_one}]
metrics:
  - {name: employees_count, display_name: Employees, description: n, aggregation: COUNT_DISTINCT,
     sql_expression: employees.emp_no, required_tables: [employees]}
dimensions:
  - {name: gender, display_name: Gender, sql_expression: employees.gender, required_tables: [employees]}
time_dimensions:
  - {name: from_date, display_name: From, sql_expression: salaries.from_date, table: salaries}
  - {name: hire_date, display_name: Hired, sql_expression: employees.hire_date, table: employees}
filter_columns:
  - {name: employees_hire_date, display_name: Hire date, sql_expression: employees.hire_date,
     required_tables: [employees], kind: date}
"""


def _two_dates():
    from app.connectors.base import SchemaContext
    from app.semantic import load_semantic_registry_from_yaml

    return load_semantic_registry_from_yaml(_TWO_DATES), SchemaContext(
        tables={"employees": [], "salaries": []}, row_counts={}, join_paths=[])


def test_a_period_applies_to_the_metrics_own_date():
    # "How many employees joined in 1990?" applied 1990 to the salary history (the schema's first
    # time dimension) through an extra join, and halved the count (benchmark, 0d).
    registry, schema = _two_dates()
    assert registry.default_time_dimension("employees_count") == "hire_date"
    from app.semantic import build_sql_from_intent

    sql = build_sql_from_intent(SemanticIntent(metric="employees_count", start_date="1990-01-01",
                                               end_date="1990-12-31"), registry)
    assert "employees.hire_date >= '1990-01-01'" in sql and "salaries" not in sql


def test_dates_named_like_the_llm_names_them_are_mapped_by_column():
    registry, schema = _two_dates()
    # A time dimension under its filter column's name.
    intent = validate_semantic_intent(SemanticIntent(metric="employees_count", time_dimension="employees_hire_date",
                                                     time_granularity="year"), registry, schema)
    assert intent.time_dimension == "hire_date"
    # A filter on the period's own time dimension becomes the period.
    from app.models.semantic_intent import FilterCondition

    intent = validate_semantic_intent(SemanticIntent(
        metric="employees_count", time_dimension="hire_date", time_granularity="quarter",
        filters=[FilterCondition(dimension="hire_date", operator="between", value=["1998-01-01", "1998-12-31"])]),
        registry, schema)
    assert intent.filters == [] and (str(intent.start_date), str(intent.end_date)) == ("1998-01-01", "1998-12-31")
    intent = validate_semantic_intent(SemanticIntent(
        metric="employees_count", filters=[FilterCondition(dimension="hire_date", operator="lt", value="1990-01-01")]),
        registry, schema)
    assert intent.filters == [] and str(intent.end_date) == "1989-12-31" and intent.start_date is None
    # The filter column over the same date is the same period.
    same = validate_semantic_intent(SemanticIntent(
        metric="employees_count",
        filters=[FilterCondition(dimension="employees_hire_date", operator="gte", value="1990-01-01")]), registry, schema)
    assert same.filters == [] and str(same.start_date) == "1990-01-01"
    # Another date is not the period: left as it was (and rejected, an unknown filter dimension).
    with pytest.raises(IntentValidationError, match="from_date"):
        validate_semantic_intent(SemanticIntent(
            metric="employees_count",
            filters=[FilterCondition(dimension="from_date", operator="gte", value="1990-01-01")]), registry, schema)
