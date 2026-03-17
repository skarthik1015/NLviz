from __future__ import annotations

from app.models.semantic_intent import SemanticIntent


def select_chart_type(intent: SemanticIntent, row_count: int) -> str:
    """Pick chart type from intent semantics.

    When both time_dimension and dimensions are present, produce a
    multi-series line chart (one line per dimension value, time on X).
    """
    if intent.time_dimension and intent.dimensions:
        return "multi_line"
    if intent.time_dimension:
        return "line"
    if intent.dimensions:
        return "bar"
    return "stat"
