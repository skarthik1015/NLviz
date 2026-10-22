"""Thresholds on a metric, per group: "albums with more than 5 tracks" (Chinook). The LLM writes a filter
on the metric's name; validation moves it to `metric_filters`, and the compiler emits HAVING."""
from __future__ import annotations

import pytest

from app.connectors.base import SchemaContext
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import SemanticRegistry, build_sql_from_intent
from app.semantic.models import SemanticDimension, SemanticJoin, SemanticMetric, SemanticSchema, SemanticTable
from app.services.intent_mapper import IntentValidationError, validate_semantic_intent

REGISTRY = SemanticRegistry(SemanticSchema(
    version="1.0", dataset="chinook", description="d", time_dimensions=[],
    tables=[SemanticTable(name="track"), SemanticTable(name="album"), SemanticTable(name="invoice_line")],
    joins=[SemanticJoin(from_table="track", to_table="album", on="track.album_id = album.album_id", join_type="LEFT"),
           SemanticJoin(from_table="invoice_line", to_table="track", on="invoice_line.track_id = track.track_id",
                        join_type="LEFT")],
    metrics=[SemanticMetric(name="tracks", display_name="Number of Tracks", description="n", aggregation="COUNT_DISTINCT",
                            sql_expression="track.track_id", required_tables=["track"]),
             SemanticMetric(name="average_milliseconds", display_name="Average Milliseconds", description="n",
                            aggregation="AVG", sql_expression="track.milliseconds", required_tables=["track"]),
             SemanticMetric(name="sales", display_name="Sales", description="n", aggregation="SUM",
                            sql_expression="invoice_line.unit_price * invoice_line.quantity",
                            required_tables=["invoice_line"])],
    dimensions=[SemanticDimension(name="album_title", display_name="Album Title", sql_expression="album.title",
                                  required_tables=["album"], cardinality="medium")]))
SCHEMA = SchemaContext(tables={"track": [], "album": [], "invoice_line": []}, row_counts={}, join_paths=[])


def _intent(metric="tracks", value=5, operator="gt", dimensions=("album_title",)):
    return SemanticIntent(metric=metric, dimensions=list(dimensions),
                          filters=[FilterCondition(dimension="tracks", operator=operator, value=value)])


def test_a_filter_on_a_metric_becomes_a_having_threshold():
    intent = validate_semantic_intent(_intent(), REGISTRY, SCHEMA)
    assert intent.filters == [] and [(t.metric, t.operator, t.value) for t in intent.metric_filters] == [("tracks", "gt", 5.0)]
    sql = build_sql_from_intent(intent, REGISTRY)
    assert "GROUP BY album.title\nHAVING COUNT(DISTINCT track.track_id) > 5.0" in sql
    # Another metric of the same table as the threshold: "average length of albums with more than 5 tracks".
    other = validate_semantic_intent(_intent(metric="average_milliseconds"), REGISTRY, SCHEMA)
    assert "HAVING COUNT(DISTINCT track.track_id) > 5.0" in build_sql_from_intent(other, REGISTRY)


@pytest.mark.parametrize("intent,reason", [
    (_intent(dimensions=()), "per group"),            # no breakdown to count within
    (_intent(value="many"), "needs a number"),
    (_intent(metric="sales"), "same table"),          # tracks joined onto sales would count only tracks that sold
])
def test_thresholds_that_cannot_be_answered_correctly_are_rejected(intent, reason):
    with pytest.raises(IntentValidationError, match=reason):
        validate_semantic_intent(intent, REGISTRY, SCHEMA)


def test_the_explanation_states_the_threshold():
    from app.agent.nodes.explainer import describe_computed

    intent = validate_semantic_intent(_intent(), REGISTRY, SCHEMA)
    assert describe_computed(intent, REGISTRY) == "Number of Tracks by Album Title, only where Number of Tracks is more than 5"
