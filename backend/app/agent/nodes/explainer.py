"""Explains a result, grounded on what was computed.

The explanation used to restate the question: asked for "average product weight by category" and
answered with average order value, it described "the average product weight"; for a single-row
result it saw no figures and invented one ("one recorded order"). Now the LLM gets what was
actually computed (metric and dimension labels, filters, date range) and figures taken from the
rows, and may describe only those.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.agent.state import AgentState

if TYPE_CHECKING:
    from app.semantic import SemanticRegistry
    from app.services.intent_mapper import IntentMapperConfig

_TOP_ROWS = 5


def build_explainer_node(config: "IntentMapperConfig", registry: "SemanticRegistry | None" = None
                         ) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        question = state.get("question", "")
        intent = state.get("intent")
        row_count = state.get("row_count", 0)
        rows = state.get("rows", [])
        validation_status = state.get("validation_status", "ok")

        if validation_status == "empty" or row_count == 0:
            mode = "empty"
            if intent is not None and (intent.start_date or intent.end_date):
                # Answered as asked (no silent retry without the dates); the description names the period.
                explanation = (f"No data for {describe_computed(intent, registry)}. "
                               "The data may not cover that period; try another.")
            else:
                explanation = (
                    "No results were found for this query. "
                    "Try broadening your criteria or rephrasing the question."
                )
        elif config.mode == "llm" and config.provider and config.model:
            explanation, mode = _llm_explanation(intent, rows, row_count, registry, config)
        else:
            explanation, mode = _template_explanation(intent, row_count, registry), "template"

        # Notes from the checks (units the figures are in) are stated as given, never paraphrased.
        notes = [item["text"] for item in state.get("evidence") or ()
                 if isinstance(item, dict) and item.get("kind") == "note" and item.get("text")]
        if notes and row_count:
            explanation = f"{explanation.rstrip()} {' '.join(dict.fromkeys(notes))}"
        return {
            "explanation": explanation,
            # Remembered for follow-up questions in the same conversation.
            "previous_turn": {"question": question, "intent": intent.model_dump(mode="json", exclude_none=True)}
            if intent is not None else state.get("previous_turn"),
            "user_trace": ["Summary generated"],
            "debug_trace": [f"Explainer: mode={mode}, row_count={row_count}"],
        }

    return node


def _label(registry, kind: str, name: str) -> str:
    try:
        return getattr(registry, f"get_{kind}")(name).display_name
    except Exception:  # no registry, or an unknown name: fall back to the name
        return name.replace("_", " ")


def describe_computed(intent, registry) -> str:
    """What the query computed, in the registry's words: "Total Revenue by Customer State, monthly
    by Order Date, where Payment Type is credit_card, from 2018-01-01 to 2018-12-31"."""
    text = _label(registry, "metric", intent.metric)
    if intent.dimensions:
        text += " by " + ", ".join(_label(registry, "dimension", d) for d in intent.dimensions)
    if intent.time_dimension:
        text += f", {intent.time_granularity or 'per period'} by {_label(registry, 'time_dimension', intent.time_dimension)}"
    words = {"eq": "equal to", "ne": "other than", "gt": "more than", "gte": "at least", "lt": "less than",
             "lte": "at most"}
    for condition in intent.filters:
        label = _label(registry, "dimension", condition.dimension)
        if condition.operator in ("is_null", "is_not_null"):
            text += f", where {label} is {'missing' if condition.operator == 'is_null' else 'present'}"
            continue
        value = ", ".join(map(str, condition.value)) if isinstance(condition.value, list) else condition.value
        try:
            unit = getattr(registry.get_dimension(condition.dimension), "unit", None)
        except Exception:  # no registry, or an unknown name
            unit = None
        if unit and condition.operator in words:
            # "where Milliseconds is more than 300000 milliseconds": the conversion is visible.
            text += f", where {label} is {words[condition.operator]} {value} {unit}"
        else:
            text += f", where {label} {condition.operator} {value}"
    for threshold in getattr(intent, "metric_filters", None) or ():
        number = int(threshold.value) if float(threshold.value).is_integer() else threshold.value
        text += f", only where {_label(registry, 'metric', threshold.metric)} is {words[threshold.operator]} {number}"
    if intent.start_date or intent.end_date:
        text += f", from {intent.start_date or 'the start'} to {intent.end_date or 'the end'}"
    return text


def _format(value) -> str:
    if isinstance(value, float):
        return "null" if math.isnan(value) else f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


_ADDITIVE = {"SUM", "COUNT", "COUNT_DISTINCT"}
_MAX_SHARE_ROWS = 10


def _row_label(row: dict, registry) -> str:
    """"Has Online Delivery = No": a bare "No" was read as "have online delivery" by the 4B model."""
    parts = []
    for key, value in row.items():
        if key == "metric_value":
            continue
        name = "Period" if key == "time_bucket" else _label(registry, "dimension", key)
        parts.append(f"{name} = {_format(value)}")
    # A single figure: "Result", not "Total" (the model wrote "Average Cost For Two ... is Total: 596.09").
    return ", ".join(parts) or "Result"


def _numeric(rows: list) -> list[tuple[dict, float]]:
    return [(r, r["metric_value"]) for r in rows
            if isinstance(r.get("metric_value"), (int, float)) and not (isinstance(r["metric_value"], float)
                                                                       and math.isnan(r["metric_value"]))]


def _shares(intent, rows: list, registry) -> dict[int, float] | None:
    """Each row's share of the total, for a small breakdown of an additive metric (counts, sums) with
    no time axis: "Has Online Delivery = No: 7,100 (74.3% of the total)"."""
    if intent is None or not intent.dimensions or intent.time_dimension or len(rows) > _MAX_SHARE_ROWS:
        return None
    try:
        if registry.get_metric(intent.metric).aggregation not in _ADDITIVE:
            return None
        # A row is counted under each item of a list dimension: the parts add up to more than the total.
        if any(registry.get_dimension(d).separator for d in intent.dimensions):
            return None
    except Exception:
        return None
    values = _numeric(rows)
    total = sum(v for _, v in values)
    if len(values) < 2 or total <= 0 or any(v < 0 for _, v in values):
        return None
    return {id(r): v / total for r, v in values}


def _facts(rows: list, row_count: int, intent=None, registry=None) -> str:
    """Figures from the rows, each with its labels, so the LLM never has to guess a number or a name."""
    # A single figure needs no row count (the model echoed "returns 1 row(s)" back).
    lines = [] if row_count == 1 and len(rows) == 1 else [f"{row_count} row(s)."]
    shares =_shares(intent, rows, registry) or {}
    for row in rows[:_TOP_ROWS]:
        share = f" ({shares[id(row)]:.1%} of the total)" if id(row) in shares else ""
        lines.append(f"{_row_label(row, registry)}: {_format(row.get('metric_value'))}{share}")
    values = _numeric(rows)
    if len(values) > 1:
        high = max(values, key=lambda item: item[1])
        low = min(values, key=lambda item: item[1])
        lines.append(f"Highest: {_row_label(high[0], registry)} ({_format(high[1])}). "
                     f"Lowest: {_row_label(low[0], registry)} ({_format(low[1])}). Across all {len(rows)} rows.")
    return "\n".join(lines)


_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    return [m.group(0).replace(",", "") for m in _NUMBER.finditer(text)]


def _grounded(explanation: str, facts: str) -> bool:
    """Every number in the explanation is one of the facts' numbers, as written or rounded (a figure
    of 4.6667 may appear as 4.67 or 4.7); small counts ("top 5", "1-2") are allowed. The 4B model
    otherwise wrote a "lowest" taken from the five rows it was shown."""
    known = [float(n) for n in _numbers(facts)]
    for token in _numbers(explanation):
        value = float(token)
        decimals = len(token.split(".")[1]) if "." in token else 0
        if value.is_integer() and 0 <= value <= 10:
            continue
        if not any(round(k, decimals) == value or k == value for k in known):
            return False
    return True


_CJK = re.compile(r"[　-鿿가-힯]")


def _foreign_script(explanation: str, facts: str) -> bool:
    """CJK characters the facts do not contain: qwen3 sometimes slips one in ("it is 的 3.38")."""
    return bool(_CJK.search(explanation)) and not _CJK.search(facts)


def _llm_explanation(intent, rows: list, row_count: int, registry, config: "IntentMapperConfig") -> tuple[str, str]:
    """(the explanation, what wrote it): "llm", or "template (...)" with the reason the LLM's text
    was not used. The trace said "llm" even when the template answered (review)."""
    try:
        from app.llm import build_completion_client, strip_json_fences

        config = config.for_task("explain")
        client = build_completion_client(config.provider)
        computed = describe_computed(intent, registry)
        facts = _facts(rows, row_count, intent, registry)
        user_prompt = (
            f"Computed: {computed}\n"
            f"Results (ordered as returned):\n{facts}\n"
            "In 1-2 sentences, summarise these results for a business user. Describe the computed measure by "
            "its name above, use only the figures above with the labels they are given, and do not call it "
            "anything else. "
            'Return JSON: {"explanation": "..."}'
        )
        raw = client.complete_json(
            system_prompt=(
                "You summarise query results. State only facts present in the input; never invent numbers, "
                'names or causes. Respond with JSON: {"explanation": "plain text"}'
            ),
            user_prompt=user_prompt,
            model=config.model,
            timeout_ms=config.timeout_ms,
        )
        parsed = json.loads(strip_json_fences(raw))
        explanation = str(parsed.get("explanation", "")).strip()
        if not explanation:
            reason = "empty answer"
        elif not _grounded(explanation, f"{computed}\n{facts}"):
            reason = "numbers not in the results"
        elif _foreign_script(explanation, facts):
            reason = "foreign script"
        else:
            return explanation, "llm"
        return _template_explanation(intent, row_count, registry, rows), f"template ({reason})"
    except Exception as exc:
        return _template_explanation(intent, row_count, registry, rows), f"template (LLM failed: {type(exc).__name__})"


def _template_explanation(intent, row_count: int, registry=None, rows: list | None = None) -> str:
    if intent is None:
        return f"Query returned {row_count} result(s)."
    computed = describe_computed(intent, registry)
    values = _numeric(rows or [])
    if len(values) == 1 and not intent.dimensions and not intent.time_dimension:
        return f"{computed}: {_format(values[0][1])}."
    if len(values) > 1:
        high = max(values, key=lambda item: item[1])
        low = min(values, key=lambda item: item[1])
        return (f"{computed}: highest {_row_label(high[0], registry)} ({_format(high[1])}), "
                f"lowest {_row_label(low[0], registry)} ({_format(low[1])}), across {row_count} result(s).")
    return f"{computed}: {row_count} result(s)."
