"""Example questions for a connection's data model, offered as chips under the question box.

Built only from combinations the data model can answer as asked: the SQL compiles (the tables join)
and the metric is not repeated across a one-to-many join. The browser used to pair the first metric
with the first dimensions and a fixed year; on Chinook that suggested "Top 10 Genre Name by Total
Invoice Amount" (refused: each invoice counts once per line) and "Customer Count in 2018" (the data
covers 2021-2025).
"""
from __future__ import annotations

from app.models import SemanticIntent
from app.semantic.loader import SemanticRegistry
from app.semantic.sql_builder import build_sql_from_intent, fan_out_joins

MAX_SUGGESTIONS = 6
_MONEY = ("amount", "revenue", "sales", "price", "cost", "value", "spend", "profit")


def _answerable(intent: SemanticIntent, registry: SemanticRegistry) -> bool:
    tables: set[str] = set()
    try:
        for name in intent.dimensions:
            tables |= {t.split()[0] for t in registry.get_dimension(name).required_tables}
        if intent.time_dimension:
            tables.add(registry.get_time_dimension(intent.time_dimension).table)
        build_sql_from_intent(intent, registry)
    except (KeyError, ValueError):
        return False
    return not fan_out_joins(intent.metric, tables, registry)


def suggested_questions(registry: SemanticRegistry) -> list[str]:
    schema = registry.schema
    # Money measures first, then other measures, then counts: "Total Invoice Line Amount by Genre"
    # says more than a row count.
    def rank(metric) -> tuple[bool, bool]:
        money = any(word in f"{metric.name} {metric.display_name}".lower() for word in _MONEY)
        return metric.aggregation in ("COUNT", "COUNT_DISTINCT"), not money

    metrics = sorted(schema.metrics, key=rank)
    breakdowns = [d for d in schema.dimensions if d.cardinality != "high"]
    names = [d for d in schema.dimensions if d.cardinality == "high"]
    suggestions: list[str] = []
    used_metrics: set[str] = set()
    used_dimensions: set[str] = set()

    def add(text: str, metric: str, dimension: str | None = None) -> None:
        if text not in suggestions and len(suggestions) < MAX_SUGGESTIONS:
            suggestions.append(text)
            used_metrics.add(metric)
            if dimension:
                used_dimensions.add(dimension)

    if metrics:
        add(f"What is the {metrics[0].display_name}?", metrics[0].name)
    for metric in metrics:
        dimension = next((d for d in breakdowns if d.name not in used_dimensions
                          and _answerable(SemanticIntent(metric=metric.name, dimensions=[d.name]), registry)), None)
        if dimension and metric.name not in used_metrics - {metrics[0].name}:
            add(f"{metric.display_name} by {dimension.display_name}", metric.name, dimension.name)
        if len(used_dimensions) >= 2:
            break
    for metric in metrics:
        name = next((d for d in names if _answerable(
            SemanticIntent(metric=metric.name, dimensions=[d.name], limit=10), registry)), None)
        if name:
            add(f"Top 10 {name.display_name} by {metric.display_name}", metric.name, name.name)
            break
    for time_dimension in schema.time_dimensions[:1]:
        for metric in metrics:
            intent = SemanticIntent(metric=metric.name, time_dimension=time_dimension.name, time_granularity="month")
            if _answerable(intent, registry):
                add(f"{metric.display_name} by month", metric.name)
                break
    return suggestions
