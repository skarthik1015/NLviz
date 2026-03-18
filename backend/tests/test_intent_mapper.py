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

    with pytest.raises(IntentValidationError, match="unknown tables"):
        router.map_with_metadata(
            "show revenue by seller state",
            registry=_load_registry(),
            schema=SchemaContext(
                tables={"customers": [], "orders": [], "order_payments": []},
                row_counts={},
                join_paths=[],
            ),
        )


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

    result = router.map_with_metadata("show revenue by state", _load_registry(), _schema_context())

    assert result.source == "llm"
    assert result.intent.metric == "total_revenue"
    assert result.trace[0].startswith("Intent mapper: source=llm")


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

    result = router.map_with_metadata("show orders by customer state", _load_registry(), _schema_context())

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

    with pytest.raises(IntentMappingError, match="LLM intent mapping failed"):
        router.map_with_metadata("show orders by state", _load_registry(), _schema_context())
