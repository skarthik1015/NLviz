"""How an answer was understood, as data for the interpretation panel (plan step 1a).

Built from the final intent and the registry, so it shows what was computed, not what was asked:
the metric with its definition, filters, thresholds, grouping, the period and the date it applies
to, ordering, and the notes the checks attached (double counting, zeros, units). `policies` lists the
business decisions behind the metric with where each came from; it stays empty until the registry
records them (step 1b).
"""
from __future__ import annotations

from app.models.responses import (Interpretation, InterpretedField, InterpretedFilter, InterpretedMetric,
                                  InterpretedPeriod)


def _label(registry, name: str) -> str:
    try:
        return registry.get_dimension(name).display_name
    except KeyError:
        return name.replace("_", " ")


def build_interpretation(intent, registry, evidence: list | None = None) -> Interpretation | None:
    from app.semantic.sql_builder import _build_metric_expression  # lazy: app.semantic imports app.models

    try:
        metric = registry.get_metric(intent.metric)
    except KeyError:
        return None
    definition = _build_metric_expression(metric) + (f" where {metric.base_filter}" if metric.base_filter else "")
    period = None
    if intent.start_date or intent.end_date:
        name = intent.time_dimension or registry.default_time_dimension(intent.metric)
        if name:
            time = registry.get_time_dimension(name)
            period = InterpretedPeriod(column=time.name, label=time.display_name, start=intent.start_date,
                                       end=intent.end_date)
    grain = None
    if intent.time_dimension:
        time = registry.get_time_dimension(intent.time_dimension)
        grain = InterpretedField(name=time.name, label=f"{time.display_name} by "
                                 f"{intent.time_granularity or time.default_granularity}")
    return Interpretation(
        path="semantic",
        metric=InterpretedMetric(name=metric.name, label=metric.display_name, aggregation=metric.aggregation,
                                 definition=definition, description=metric.description),
        filters=[InterpretedFilter(dimension=c.dimension, label=_label(registry, c.dimension), operator=c.operator,
                                   value=c.value) for c in intent.filters],
        thresholds=[f"{t.metric} {t.operator} {t.value:g}" for t in intent.metric_filters],
        grouping=[InterpretedField(name=d, label=_label(registry, d)) for d in intent.dimensions],
        time_grain=grain,
        period=period,
        order_by=intent.order_by,
        limit=intent.limit,
        notes=[item["text"] for item in evidence or () if isinstance(item, dict) and item.get("kind") == "note"
               and item.get("text")],
    )


def interpretation_from_sql(sql: str, verdict: dict | None, registry, columns_by_table) -> Interpretation:
    """The panel for the SQL-primary path, read from the SQL that ran: the layer's metrics it
    computes, its conditions, grouping, order and limit, the definitions applied, and the checks'
    notes."""
    from app.models.responses import InterpretedPolicy
    from app.services.sql_checks import describe

    verdict = verdict or {}
    # The conditions each metric actually ran with, when a definition was decided: one structured
    # list per policy ([]: every row). The data model's description assumes its own condition: it is
    # shown only when exactly that ran.
    ran_with: dict[str, list[str]] = {}
    for policy in verdict.get("policies") or ():
        if "conditions" in policy:
            for name in policy.get("metrics") or [policy["name"]]:
                ran_with.setdefault(name, []).extend(c for c in policy["conditions"] if c not in ran_with.get(name, []))
    measures = []
    for found in verdict.get("measures") or ():
        if any(m.name == found["name"] for m in measures):
            continue
        metric = registry.get_metric(found["name"])
        definition, description = found["sql"], metric.description
        if metric.name in ran_with:
            conditions = ran_with[metric.name]
            definition += f" where {' AND '.join(conditions)}" if conditions else " over every row"
            if conditions != ([metric.base_filter] if metric.base_filter else []):
                description = ""
        measures.append(InterpretedMetric(name=metric.name, label=metric.display_name, aggregation=metric.aggregation,
                                          definition=definition, description=description))
    try:
        described = describe(sql, registry, columns_by_table)
    except Exception:  # noqa: BLE001 - the panel must never break an answer
        described = {"filters": [], "grouping": [], "order_by": None, "limit": None, "partial": True}
    notes = list(verdict.get("notes") or ())
    if described.get("partial"):
        notes.append("Part of this query (a subquery, HAVING or a window function) is not listed here; "
                     "the SQL below is the full account.")
    if not measures:
        notes.append("The query computes no metric defined in the data model; check its SQL.")
    return Interpretation(
        path="sql",
        sql=sql,
        metric=measures[0] if measures else None,
        measures=measures,
        filters=[InterpretedFilter(**f) for f in described["filters"]],
        grouping=[InterpretedField(**g) for g in described["grouping"]],
        order_by=described["order_by"] or "",
        limit=described["limit"] or 0,
        policies=[InterpretedPolicy(**p) for p in verdict.get("policies") or ()],
        notes=notes,
    )
