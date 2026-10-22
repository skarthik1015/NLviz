"""Ambiguities the data reveals, asked about instead of answered with a meaningless number (Phase 4).

A check looks at a checked intent and, when the answer would mix things that must not be mixed,
returns a "choice" clarification: a prompt and a few choices, each a small change to the intent
(`patch`). The clarify node applies the chosen patch and the turn continues; the check then passes
because the patched intent is no longer ambiguous.

First check: mixed units. "Average cost for two" over a restaurant file averaged amounts in 12
currencies (Indian Rupees, Dollars, Indonesian Rupiah...) into one figure. A metric whose schema
names a `unit_dimension` is probed with that dimension added to the breakdown; when any result
group would hold more than one unit, the user picks a unit or a breakdown by unit. Per-country
averages pass (one currency per country); a global average does not.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from app.models import SemanticIntent

logger = logging.getLogger(__name__)

Probe = Callable[[SemanticIntent], list[dict]]

MAX_UNIT_CHOICES = 6
BREAK_DOWN = "by"
PROBE_ROWS = 5000


class NoProbe(RuntimeError):
    """No data to check with (offline tests, explicit intents without a connector): data checks skip."""


def _label(registry, kind: str, name: str) -> str:
    try:
        return getattr(registry, f"get_{kind}")(name).display_name
    except Exception:
        return name.replace("_", " ")


def mixed_units(intent: SemanticIntent, registry, probe: Probe) -> dict | None:
    """A choice clarification when the metric would add or average different units; a note naming
    the unit when it would not; None for a metric without units."""
    try:
        unit = registry.get_metric(intent.metric).unit_dimension
    except Exception:
        return None
    if not unit or unit in intent.dimensions:
        return None
    fixed = next((c for c in intent.filters if c.dimension == unit and c.operator == "eq"), None)
    if fixed is not None:
        return {"kind": "note", "dimension": unit, "text": f"Amounts are in {fixed.value}."}
    unit_label = _label(registry, "dimension", unit)
    try:
        rows = probe(intent.model_copy(update={"dimensions": [*intent.dimensions, unit], "limit": PROBE_ROWS}))
    except NoProbe:
        raise
    except Exception as exc:  # noqa: BLE001 - unproven is not single-unit (review B6)
        logger.warning("Unit probe failed for %s: %s", intent.metric, exc)
        return _units_unproven(intent, registry, unit, unit_label)
    if len(rows) >= PROBE_ROWS:
        # Truncated: a group cut off here could hold a second unit.
        return _units_unproven(intent, registry, unit, unit_label)
    groups: dict[tuple, set] = {}
    for row in rows:
        key = tuple(row.get(d) for d in intent.dimensions) + ((row.get("time_bucket"),) if intent.time_dimension else ())
        value = row.get(unit)
        if value is not None:
            groups.setdefault(key, set()).add(str(value))
    if not groups:
        return None
    units = sorted({value for values in groups.values() for value in values})
    if all(len(values) == 1 for values in groups.values()):
        # Not ambiguous, but the figures carry no unit of their own: say which.
        text = (f"Amounts are in {units[0]}." if len(units) == 1 else
                f"Each row is in its own {unit_label} ({len(units)} in all); compare rows only within one {unit_label}.")
        return {"kind": "note", "dimension": unit, "text": text}
    metric_label = _label(registry, "metric", intent.metric)
    ordered = _by_rows(units, intent, registry, unit, probe)
    choices = [{"id": BREAK_DOWN, "label": f"Break down by {unit_label}", "patch": {"add_dimension": unit}}]
    choices += [{"id": str(i), "label": value,
                 "patch": {"add_filter": {"dimension": unit, "operator": "eq", "value": value}}}
                for i, value in enumerate(ordered[:MAX_UNIT_CHOICES])]
    return {
        "kind": "choice",
        "reason": "mixed_units",
        "dimension": unit,
        "prompt": (f"{metric_label} is recorded in {len(ordered)} different {unit_label} values here, so one "
                   f"figure across them would be meaningless. Pick one {unit_label}, or break the result "
                   f"down by {unit_label}."),
        "choices": choices,
    }


def _units_unproven(intent: SemanticIntent, registry, unit: str, unit_label: str) -> dict:
    """When the units could not be checked: ask, offering the breakdown by unit, which never mixes."""
    metric_label = _label(registry, "metric", intent.metric)
    return {
        "kind": "choice",
        "reason": "mixed_units",
        "dimension": unit,
        "prompt": (f"{metric_label} may be recorded in several {unit_label} values, and I could not check "
                   f"which ones this question covers. Break the result down by {unit_label}?"),
        "choices": [{"id": BREAK_DOWN, "label": f"Break down by {unit_label}", "patch": {"add_dimension": unit}}],
    }


def _by_rows(units: list[str], intent: SemanticIntent, registry, unit: str, probe: Probe) -> list[str]:
    """Units with the most rows first (Indian Rupees, 8,652 rows, before Botswana Pula, 22), counted
    with a COUNT metric on the same table under the same filters; alphabetical if there is none."""
    try:
        table = registry.get_metric(intent.metric).required_tables[0]
        counter = next(m.name for m in registry.schema.metrics
                       if m.aggregation in ("COUNT", "COUNT_DISTINCT") and m.required_tables[:1] == [table])
        rows = probe(SemanticIntent(metric=counter, dimensions=[unit], filters=intent.filters,
                                    start_date=intent.start_date, end_date=intent.end_date, limit=5000))
        counts = {str(row.get(unit)): row.get("metric_value") or 0 for row in rows}
        return sorted(units, key=lambda value: (-counts.get(value, 0), value))
    except Exception:  # noqa: BLE001 - ordering is cosmetic
        return units


# Not "total": that is the aggregation's word ("total quantity" is not money).
_MONEY = ("amount", "revenue", "sales", "price", "cost", "value", "spend")


def _question_tables(intent: SemanticIntent, registry) -> set[str]:
    tables: set[str] = set()
    for name in [*intent.dimensions, *(c.dimension for c in intent.filters)]:
        try:
            tables |= {t.split()[0] for t in registry.get_dimension(name).required_tables}
        except Exception:  # noqa: BLE001 - an unknown name is the validator's business
            continue
    if intent.time_dimension or intent.start_date or intent.end_date:
        try:
            name = intent.time_dimension or registry.default_time_dimension(intent.metric)
            tables.add(registry.get_time_dimension(name).table)
        except Exception:  # noqa: BLE001
            pass
    return tables


SAME_TOTAL_TOLERANCE = 0.005


def _same_total(metric: str, alternative: str, probe: Probe) -> bool:
    """Whether two metrics have the same grand total (invoice totals and invoice-line amounts both
    2,328.60 on Chinook). False when either cannot be computed."""
    try:
        totals = [float(probe(SemanticIntent(metric=name))[0]["metric_value"]) for name in (metric, alternative)]
    except Exception:  # noqa: BLE001 - no evidence: ask rather than substitute
        return False
    return abs(totals[0] - totals[1]) <= SAME_TOTAL_TOLERANCE * max(abs(totals[0]), abs(totals[1]), 1e-9)


def fan_out(intent: SemanticIntent, registry, probe: Probe) -> dict | None:
    """A metric repeated across a one-to-many join gives an inflated number with no error: invoice
    totals summed by genre counted each invoice once per line (Rock 7,720 for 827). Offer the
    metrics recorded on the "many" side, or refuse when there is none."""
    from app.semantic.sql_builder import fan_out_joins

    tables = _question_tables(intent, registry)
    fanned = fan_out_joins(intent.metric, tables, registry)
    if not fanned:
        return None
    metric = registry.get_metric(intent.metric)
    many = {pair[1] for pair in fanned}
    one_label = fanned[0][0].replace("_", " ")
    many_label = fanned[0][1].replace("_", " ")
    money = any(word in f"{metric.name} {metric.display_name}".lower() for word in _MONEY)
    alternatives = []
    for m in registry.schema.metrics:
        if m.name == metric.name or not m.required_tables or m.required_tables[0].split()[0] not in many:
            continue
        if m.aggregation != metric.aggregation or fan_out_joins(m.name, tables, registry):
            continue
        text = f"{m.name} {m.display_name}".lower()
        alternatives.append((0 if money and any(w in text for w in _MONEY) else 1, m.display_name, m.name))
    alternatives.sort()
    metric_label = metric.display_name
    money_alternatives = [a for a in alternatives if a[0] == 0]
    if money and len(money_alternatives) == 1 and _same_total(metric.name, money_alternatives[0][2], probe):
        # One money measure recorded per line, and the two add up to the same total: the same amount
        # at a finer level ("revenue by genre" is the line amounts by genre). Used, and said why.
        # Not when the totals differ: shipping fees are not the sales on an order's lines.
        _, label, name = money_alternatives[0]
        return {"kind": "substitute", "reason": "fan_out", "patch": {"set_metric": name},
                "text": (f"Used {label}: {metric_label} is recorded once per {one_label}, and one {one_label} "
                         f"has several {many_label} rows, so it would count each {one_label} several times.")}
    if not alternatives and metric.aggregation == "AVG":
        # The compiler averages at the "one" grain (sql_builder._average_grain_keys): each order once
        # per group, not once per item. Said, because a group can share orders with another.
        return {"kind": "note", "dimension": "fan_out",
                "text": (f"{metric_label} is averaged per {one_label}: each {one_label} counts once in "
                         f"every group it has {many_label} rows in.")}
    if not alternatives:
        return {"kind": "refuse", "reason": "fan_out",
                "text": (f"{metric_label} is recorded once per {one_label}, and one {one_label} has several "
                         f"{many_label} rows here, so this breakdown would count each {one_label} several times. "
                         f"Break it down by something recorded per {one_label} instead.")}
    return {
        "kind": "choice",
        "reason": "fan_out",
        "dimension": None,
        "prompt": (f"{metric_label} is recorded once per {one_label}, and one {one_label} has several "
                   f"{many_label} rows, so splitting it this way would count each {one_label} several times. "
                   f"Use a measure recorded per {many_label} instead:"),
        "choices": [{"id": str(i), "label": label, "patch": {"set_metric": name}}
                    for i, (_, label, name) in enumerate(alternatives[:4])],
    }


ZERO_EXCLUDE = "exclude_zeros"
ZERO_INCLUDE = "include_zeros"


def zero_placeholder(intent: SemanticIntent, registry, probe: Probe) -> dict | None:
    """An average whose column has many zeros that may mean "not recorded": ask whether to leave them
    out. Profiling only proposes the variant; whether 0 is a real value is the user's call."""
    variant = next((m for m in registry.schema.metrics if getattr(m, "excludes_zeros_of", None) == intent.metric),
                   None)
    if variant is None:
        return None
    metric_label = _label(registry, "metric", intent.metric)
    share = f"{variant.zero_share:.0%} of rows" if variant.zero_share else "many rows"
    return {
        "kind": "choice",
        "reason": "zero_placeholder",
        "dimension": None,
        "prompt": (f"{metric_label} is 0 in {share}. If 0 means \"not recorded\" there, "
                   "averaging those rows in pulls the figure down. Leave them out?"),
        "choices": [{"id": ZERO_EXCLUDE, "label": "Leave out the zeros (0 means not recorded)",
                     "patch": {"set_metric": variant.name}},
                    {"id": ZERO_INCLUDE, "label": "Keep them (0 is a real value)", "patch": {}}],
    }


CHECKS: tuple[Callable[[SemanticIntent, object, Probe], dict | None], ...] = (fan_out, mixed_units, zero_placeholder)


def find_ambiguity(intent: SemanticIntent, registry, probe: Probe | None, answered: frozenset[str] = frozenset()
                   ) -> tuple[dict | None, list[dict], list[str], SemanticIntent]:
    """(the first ambiguity any check finds, notes for the answer, trace lines, the intent). A note is
    a {"kind": "note", "text"} the explanation repeats ("Amounts are in Indian Rupees(Rs.)"). A
    "substitute" applies its patch, adds its text as a note, and the later checks see the patched
    intent. Without a probe, checks that need data are skipped; with one, a check that fails refuses
    the question rather than answer it unchecked (review B6)."""
    notes: list[dict] = []
    trace: list[str] = []
    if probe is None:
        probe = _no_probe  # checks that need no data still run
    for check in CHECKS:
        try:
            found = check(intent, registry, probe)
        except NoProbe:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ambiguity check %s failed: %s", check.__name__, exc)
            return ({"kind": "refuse", "reason": f"{check.__name__}_failed",
                     "text": "I could not check this question for double counting or mixed units, so I "
                             "won't answer it unchecked. Please try again."},
                    notes, [*trace, f"Check intent: {check.__name__} failed: {str(exc)[:200]}"], intent)
        if found and found.get("kind") == "choice" and found.get("reason") in answered:
            continue  # the user already chose this turn ("keep the zeros" leaves the intent as it was)
        if found and found.get("kind") == "note":
            notes.append(found)
        elif found and found.get("kind") == "substitute":
            intent, _ = apply_choice(intent, {"choices": [{"id": "0", "label": "", "patch": found["patch"]}]}, "0")
            notes.append({"kind": "note", "dimension": "metric", "text": found["text"]})
            trace.append(f"Check intent: {found['reason']}: {found['text']}")
        elif found:
            return found, notes, [*trace, f"Check intent: ambiguous ({found['reason']}): asking the user"], intent
    return None, notes, trace, intent


def _no_probe(intent: SemanticIntent) -> list[dict]:
    raise NoProbe("no probe")


def apply_choice(intent: SemanticIntent, pending: dict, answer) -> tuple[SemanticIntent, str]:
    """The intent with the chosen patch applied, and the chosen label. An unknown answer takes the
    first choice (for mixed units: the breakdown, which never mixes anything)."""
    from app.models.semantic_intent import FilterCondition

    choices = pending["choices"]
    chosen = next((c for c in choices if c["id"] == str(answer)), choices[0])
    patch = chosen["patch"]
    if "set_metric" in patch:
        intent = intent.model_copy(update={"metric": patch["set_metric"]})
    if "add_dimension" in patch and patch["add_dimension"] not in intent.dimensions:
        intent = intent.model_copy(update={"dimensions": [*intent.dimensions, patch["add_dimension"]]})
    if "add_filter" in patch:
        condition = FilterCondition(**patch["add_filter"])
        filters = [c for c in intent.filters if c.dimension != condition.dimension]
        intent = intent.model_copy(update={"filters": [*filters, condition]})
    return intent, chosen["label"]
