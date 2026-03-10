from __future__ import annotations

from app.models.semantic_intent import SemanticIntent


def select_chart_type(intent: SemanticIntent, row_count: int) -> str:
    """Pick chart type from intent semantics — never from data shape."""
    if intent.time_dimension:
        return "line"
    if intent.dimensions:
        return "bar"
    return "stat"
