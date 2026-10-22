from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import dataclasses
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable, Literal, Protocol

from pydantic import ValidationError

from app.connectors.base import SchemaContext
from app.llm import (
    LLMCompletionClient,
    LLMError,
    build_completion_client,
    is_budget_exceeded,
    strip_json_fences,
    structured_completion,
)
from app.models.semantic_intent import SemanticIntent
from app.semantic import SemanticRegistry

if TYPE_CHECKING:
    from app.grounding.examples import ExampleIndex
    from app.grounding.values import ValueIndex

logger = logging.getLogger(__name__)

IntentSource = Literal["heuristic", "llm", "llm_fallback", "explicit"]


class IntentMappingError(LLMError):
    pass


class IntentValidationError(ValueError):
    pass


def _answer_path(value: str) -> str:
    path = value.strip().lower() or "semantic"
    if path not in {"semantic", "sql"}:
        raise ValueError("ANSWER_PATH must be one of: semantic, sql")
    return path


@dataclass(frozen=True)
class IntentMapperConfig:
    mode: Literal["heuristic", "llm"] = "llm"
    provider: Literal["anthropic", "openai", "bedrock"] | None = "anthropic"
    model: str | None = "claude-sonnet-4-5"
    timeout_ms: int = 8000
    fallback_to_heuristic: bool = True
    debug_logging: bool = False
    # The check rules that react to the question's wording (app.agent.nodes.intent_mapper
    # PHRASE_REWRITES). PHRASE_REWRITES=off turns them all off, for the ablation in docs/ablation-results.md.
    phrase_rewrites: bool = True
    # SQL_FALLBACK=on: a question the semantic layer cannot answer, or that cannot be interpreted, is
    # answered with SQL the model writes (guarded, labelled generated_sql). Off by default.
    sql_fallback: bool = False
    # ANSWER_PATH=sql (the app's default since 2026-10-19, docs/architecture/current-architecture.md): questions
    # are answered by guarded SQL held to the semantic layer (app.agent.nodes.sql_primary);
    # "semantic": the intent and compiler. Explicit intents, and heuristic mode (no model to write
    # SQL), always use the compiler. The field's own default stays "semantic" for code that builds a
    # config directly (the tests).
    answer_path: str = "semantic"
    # DEFINITION_CLARIFICATIONS=on (the app's default): on the SQL path, an inferred metric condition
    # that changes the answer is asked about unless confirmed or chosen (app.services.sql_checks).
    # Code that builds a config directly (the tests) keeps the inferred condition unless it opts in.
    ask_definitions: bool = False

    @classmethod
    def from_env(cls) -> "IntentMapperConfig":
        mode = os.getenv("INTENT_MODE", "llm").strip().lower() or "llm"
        if mode not in {"heuristic", "llm"}:
            raise ValueError("INTENT_MODE must be one of: heuristic, llm")

        provider_value = os.getenv("LLM_PROVIDER", "anthropic").strip().lower() or None
        if provider_value and provider_value not in {"anthropic", "openai", "bedrock"}:
            raise ValueError("LLM_PROVIDER must be one of: anthropic, openai, bedrock")

        return cls(
            mode=mode,
            provider=provider_value,  # type: ignore[arg-type]
            model=os.getenv("LLM_MODEL", "claude-sonnet-4-5").strip() or None,
            timeout_ms=int(os.getenv("LLM_INTENT_TIMEOUT_MS", "8000")),
            fallback_to_heuristic=_parse_bool(os.getenv("LLM_INTENT_FALLBACK", "true")),
            debug_logging=_parse_bool(os.getenv("INTENT_DEBUG_LOGGING", "false")),
            phrase_rewrites=_parse_bool(os.getenv("PHRASE_REWRITES", "on")),
            sql_fallback=_parse_bool(os.getenv("SQL_FALLBACK", "off")),
            answer_path=_answer_path(os.getenv("ANSWER_PATH", "sql")),
            ask_definitions=_parse_bool(os.getenv("DEFINITION_CLARIFICATIONS", "on")),
        )

    def for_task(self, task: str) -> "IntentMapperConfig":
        """This config with LLM_PROVIDER_<TASK> / LLM_MODEL_<TASK> applied when set (tasks in
        app.llm.TASKS), so a task can use another model, e.g. a hosted one for schema generation."""
        from app.llm import TASKS

        if task not in TASKS:
            raise ValueError(f"Unknown LLM task {task!r}; expected one of {', '.join(TASKS)}")
        provider = os.getenv(f"LLM_PROVIDER_{task.upper()}", "").strip().lower() or self.provider
        model = os.getenv(f"LLM_MODEL_{task.upper()}", "").strip() or self.model
        if provider == self.provider and model == self.model:
            return self
        if provider not in {"anthropic", "openai", "bedrock"}:
            raise ValueError(f"LLM_PROVIDER_{task.upper()} must be one of: anthropic, openai, bedrock")
        return dataclasses.replace(self, provider=provider, model=model)  # type: ignore[arg-type]


@dataclass(frozen=True)
class IntentMappingResult:
    intent: SemanticIntent
    source: IntentSource
    trace: list[str]


@dataclass(frozen=True)
class IntentProposal:
    """One mapping attempt before validation: the intent the mapper proposed, or why it could not."""
    intent: SemanticIntent | None
    source: Literal["heuristic", "llm"]
    error: str | None
    trace: list[str]
    value_hints: list[dict] | None = None
    examples: list[dict] | None = None  # [{question, intent}], reused by repair rounds


# The model answered in a shape Instructor cannot read (several tool calls): a formatting slip,
# retried once on the spot rather than sent around the repair loop with nothing to correct.
_FORMAT_FAILURES = ("multiple tool calls",)


class IntentMapper(Protocol):
    """An LLM mapper. The router passes the keyword arguments only when it has them (value hints
    from grounding, the conversation/repair context, retrieved examples)."""

    def map(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
        value_hints: list[dict] | None = None,
        context: str | None = None,
        examples: list[dict] | None = None,
    ) -> SemanticIntent:
        ...


# Older names, used across the codebase before app.llm existed.
_build_completion_client = build_completion_client
_strip_json_fences = strip_json_fences


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_table_ref(table_ref: str) -> str:
    parts = table_ref.split()
    if not parts:
        raise IntentValidationError("Encountered an empty required_tables entry in the semantic registry")
    return parts[0]


def _schema_table_names(registry: SemanticRegistry, schema: SchemaContext) -> set[str]:
    if schema.tables:
        return set(schema.tables)
    return {table.name for table in registry.schema.tables}


def validate_semantic_intent(
    intent: SemanticIntent,
    registry: SemanticRegistry,
    schema: SchemaContext,
) -> SemanticIntent:
    errors: list[str] = []
    available_tables = _schema_table_names(registry, schema)
    intent = _time_references(intent, registry)
    intent, threshold_errors = _split_metric_filters(intent, registry)
    errors += threshold_errors

    try:
        metric = registry.get_metric(intent.metric)
    except KeyError:
        errors.append(f"Unknown metric '{intent.metric}'")
        metric = None

    if len(intent.dimensions) != len(set(intent.dimensions)):
        errors.append("Dimensions must be unique")
    # A breakdown a person can read and a chart can draw: the 4B model sometimes lists every
    # dimension ("revenue from voucher payments by month" grouped by all seven).
    max_dimensions = 1 if intent.time_dimension else 2
    if len(intent.dimensions) > max_dimensions:
        errors.append(
            f"At most {max_dimensions} dimension(s) {'with a time dimension' if intent.time_dimension else ''}"
            f"; got {len(intent.dimensions)}. Use only the breakdowns the question asks for"
        )

    for dimension_name in intent.dimensions:
        try:
            dimension = registry.get_dimension(dimension_name)
        except KeyError:
            errors.append(f"Unknown dimension '{dimension_name}'")
            continue
        if registry.is_filter_only(dimension_name):
            errors.append(f"'{dimension_name}' is a column filter: use it in filters (a condition), "
                          "not as a breakdown")
            continue

        parsed_tables = [_parse_table_ref(t) for t in dimension.required_tables]
        missing_tables = sorted(t for t in parsed_tables if t not in available_tables)
        if missing_tables:
            errors.append(
                f"Dimension '{dimension_name}' depends on unknown tables: {', '.join(missing_tables)}"
            )

    for filter_condition in intent.filters:
        try:
            filter_dimension = registry.get_dimension(filter_condition.dimension)
        except KeyError:
            errors.append(f"Unknown filter dimension '{filter_condition.dimension}'")
            continue

        parsed_tables = [_parse_table_ref(t) for t in filter_dimension.required_tables]
        missing_tables = sorted(t for t in parsed_tables if t not in available_tables)
        if missing_tables:
            errors.append(
                f"Filter dimension '{filter_condition.dimension}' depends on unknown tables: {', '.join(missing_tables)}"
            )
        problem = _filter_value_problem(filter_condition, filter_dimension)
        if problem:
            errors.append(problem)

    if metric is not None:
        parsed_tables = [_parse_table_ref(t) for t in metric.required_tables]
        missing_tables = sorted(t for t in parsed_tables if t not in available_tables)
        if missing_tables:
            errors.append(
                f"Metric '{intent.metric}' depends on unknown tables: {', '.join(missing_tables)}"
            )

    if intent.time_dimension:
        try:
            time_dimension = registry.get_time_dimension(intent.time_dimension)
        except KeyError:
            errors.append(f"Unknown time dimension '{intent.time_dimension}'")
        else:
            if time_dimension.table not in available_tables:
                errors.append(
                    f"Time dimension '{intent.time_dimension}' depends on unknown table '{time_dimension.table}'"
                )

    if intent.time_granularity and not intent.time_dimension:
        errors.append("time_granularity requires time_dimension")
    if intent.order_by.startswith("time_") and not intent.time_dimension:
        errors.append(f"order_by '{intent.order_by}' requires time_dimension")
    if intent.start_date and intent.end_date and intent.start_date > intent.end_date:
        errors.append("start_date must be on or before end_date")

    if not errors:
        # Every name exists, but the tables may not join: on Chinook an employee breakdown of invoice
        # revenue had no join path and failed at compile time as a bare 400. Rejected here, the
        # repair loop asks the LLM again with the reason.
        from app.semantic import build_sql_from_intent  # lazy: app.semantic imports app.models

        try:
            build_sql_from_intent(intent, registry)
        except ValueError as exc:
            errors.append(f"{exc}. Use only dimensions and filters whose tables join to the metric's table")

    if errors:
        raise IntentValidationError("; ".join(errors))

    return intent


def _iso_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _time_references(intent: SemanticIntent, registry: SemanticRegistry) -> SemanticIntent:
    """Dates named the way the LLM names them, mapped by column, not by wording (benchmark, 0d):
    - a time dimension given by the name of a dimension or filter column over the same column
      ("employees_hire_date" for the time dimension "hire_date") becomes that time dimension;
    - a filter on the time dimension the period applies to ("order_date between 1998-01-01 and
      1998-12-31") becomes the intent's start/end dates. Both were rejected as unknown names, twice,
      and the keyword fallback then answered without the year."""
    update: dict = {}
    if intent.time_dimension and registry.time_dimension_for(intent.time_dimension) not in (None, intent.time_dimension):
        update["time_dimension"] = registry.time_dimension_for(intent.time_dimension)
    period = update.get("time_dimension", intent.time_dimension) or registry.default_time_dimension(intent.metric)
    start, end = intent.start_date, intent.end_date
    kept = []
    dimension_names = {d.name for d in registry.schema.dimensions}
    for condition in intent.filters:
        is_time = (period is not None and condition.dimension not in dimension_names
                   and registry.time_dimension_for(condition.dimension) == period)
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        days = [_iso_day(v) for v in values]
        if not is_time or not days or None in days:
            kept.append(condition)
            continue
        low, high = {"between": (days[0], days[-1]), "in": (min(days), max(days)), "eq": (days[0], days[0]),
                     "gte": (days[0], None), "gt": (days[0] + timedelta(days=1), None),
                     "lte": (None, days[0]), "lt": (None, days[0] - timedelta(days=1))}.get(condition.operator, (None, None))
        if low is None and high is None:
            kept.append(condition)
            continue
        start = start or low
        end = end or high
    if len(kept) != len(intent.filters):
        update.update(filters=kept, start_date=start, end_date=end)
    return intent.model_copy(update=update) if update else intent


_THRESHOLD_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}
_NULL_OPERATORS = {"is_null", "is_not_null"}


def _filter_value_problem(condition, target) -> str | None:
    """A condition the compiler would turn into wrong or failing SQL: is_null with a value is fine
    (ignored), but a comparison needs a value, a number column a number, and a date column a date."""
    if condition.operator in _NULL_OPERATORS:
        return None
    if condition.value is None:
        return f"Filter on '{condition.dimension}' with operator '{condition.operator}' needs a value"
    kind = getattr(target, "kind", None)
    values = condition.value if isinstance(condition.value, list) else [condition.value]
    if kind == "number":
        for value in values:
            try:
                float(value)
            except (TypeError, ValueError):
                return (f"'{condition.dimension}' holds numbers{' in ' + target.unit if target.unit else ''}: "
                        f"compare it with a number, not {value!r}")
    if kind == "date":
        for value in values:
            try:
                date.fromisoformat(str(value)[:10])
            except ValueError:
                return f"'{condition.dimension}' holds dates: compare it with an ISO date, not {value!r}"
    return None


def _split_metric_filters(intent: SemanticIntent, registry: SemanticRegistry) -> tuple[SemanticIntent, list[str]]:
    """A filter on a metric's name is a threshold per group ("albums with more than 5 tracks": the
    LLM wrote tracks > 5 and the intent was rejected as an unknown filter dimension, twice, before
    the keyword fallback answered without the threshold). It moves to `metric_filters` (HAVING)."""
    from app.models.semantic_intent import MetricFilter

    metric_names = {m.name for m in registry.schema.metrics}
    dimension_names = {d.name for d in registry.schema.dimensions}
    kept, thresholds, errors = [], list(intent.metric_filters), []
    for condition in intent.filters:
        if condition.dimension in dimension_names or condition.dimension not in metric_names:
            kept.append(condition)
            continue
        value = condition.value
        if condition.operator not in _THRESHOLD_OPERATORS or isinstance(value, (bool, list)):
            errors.append(f"A threshold on metric '{condition.dimension}' needs one of {sorted(_THRESHOLD_OPERATORS)} "
                          "and one number")
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            errors.append(f"A threshold on metric '{condition.dimension}' needs a number, got {value!r}")
            continue
        thresholds.append(MetricFilter(metric=condition.dimension, operator=condition.operator, value=number))
    if thresholds and not intent.dimensions and not intent.time_dimension:
        errors.append("A threshold on a metric ('more than 5 tracks') applies per group: add the breakdown it "
                      "counts within (for example by album)")
    home = registry.get_metric(intent.metric).required_tables[:1] if intent.metric in metric_names else []
    for threshold in thresholds:
        threshold_metric = registry.get_metric(threshold.metric)
        if threshold_metric.base_filter:
            errors.append(f"Metric '{threshold.metric}' cannot be used as a threshold")
        elif threshold_metric.required_tables[:1] != home:
            # Joining another table's count onto this metric changes what is counted (only tracks that
            # sold, for revenue of albums with more than 5 tracks): not answered rather than answered wrong.
            errors.append(f"A threshold on '{threshold.metric}' can only be combined with a metric from the same "
                          f"table; use '{threshold.metric}' itself as the metric")
    if not thresholds and kept == intent.filters:
        return intent, errors
    return intent.model_copy(update={"filters": kept, "metric_filters": thresholds}), errors


def _build_dynamic_metric_keywords(
    registry: SemanticRegistry,
) -> list[tuple[str, list[str]]]:
    """Generate keyword tuples dynamically from registry metrics.

    Uses display_name and name parts only — description words are intentionally
    excluded to avoid cross-metric contamination (e.g. "orders" appearing in a
    revenue metric description).  Plural forms are added for name parts so that
    "orders" matches order_count without requiring an exact singular match.
    """
    result: list[tuple[str, list[str]]] = []
    for metric in registry.schema.metrics:
        keywords: list[str] = []
        keywords.append(metric.display_name.lower())
        keywords.extend(metric.name.replace("_", " ").lower().split())
        keywords.append(metric.name.replace("_", " ").lower())
        # Also add simple plural forms of name parts to match question variants
        for word in metric.name.split("_"):
            if len(word) > 3 and not word.endswith("s"):
                keywords.append(word.lower() + "s")
        result.append((metric.name, list(dict.fromkeys(keywords))))
    return result


_GROUPING_TAILS: frozenset[str] = frozenset({
    "state", "type", "status", "category", "score", "rate", "method",
    "code", "name", "city", "country", "region",
})


def _build_dynamic_dimension_keywords(
    registry: SemanticRegistry,
) -> list[tuple[str, list[str]]]:
    """Generate keyword tuples dynamically from registry dimensions.

    Two key constraints:
    1. Tail words (e.g. "state" in customer_state) are only added as standalone
       keywords for the FIRST dimension that uses them; duplicates must be
       reached via compound phrase match ("seller state" → seller_state).
    2. Head words (e.g. "customer") are NOT added as standalones for dimensions
       with a grouping tail (state, type, status, category, score, …) because
       the same head word typically belongs to multiple dimensions in that
       family (customer_state, customer_id).  Only entity-identifier dims whose
       tail is NOT a grouping suffix get head word + plural added.
    """
    claimed_tail_words: set[str] = set()
    result: list[tuple[str, list[str]]] = []
    for dim in registry.schema.dimensions:
        keywords: list[str] = []
        keywords.append(dim.display_name.lower())
        keywords.append(dim.name.replace("_", " ").lower())
        keywords.append(f"by {dim.display_name.lower()}")
        keywords.append(f"per {dim.display_name.lower()}")
        parts = dim.name.split("_")
        tail_word = parts[-1].lower() if len(parts) > 1 else ""
        is_grouping_dim = tail_word in _GROUPING_TAILS
        for i, word in enumerate(parts):
            if len(word) <= 2:
                continue
            word_lower = word.lower()
            is_tail = i == len(parts) - 1 and len(parts) > 1
            if is_tail:
                if word_lower not in claimed_tail_words:
                    keywords.append(word_lower)
                    claimed_tail_words.add(word_lower)
            else:
                # Head / middle word: only add as standalone for non-grouping dims
                if not is_grouping_dim:
                    keywords.append(word_lower)
                    if not word_lower.endswith("s"):
                        keywords.append(word_lower + "s")
        result.append((dim.name, list(dict.fromkeys(keywords))))
    return result


def _contains_phrase(question: str, phrase: str) -> bool:
    escaped = re.escape(phrase)
    return re.search(rf"(?<!\w){escaped}(?!\w)", question) is not None


def _find_metric(question: str, registry: SemanticRegistry) -> str | None:
    """Return the best-matching metric name using longest-keyword-match scoring.

    Longest match wins so compound phrases like "average order value" (19 chars)
    beat a single word like "order" (5 chars), regardless of iteration order.
    """
    metric_keywords = _build_dynamic_metric_keywords(registry)
    best_metric: str | None = None
    best_score: int = 0
    for metric_name, keywords in metric_keywords:
        for keyword in keywords:
            if _contains_phrase(question, keyword) and len(keyword) > best_score:
                best_score = len(keyword)
                best_metric = metric_name
    return best_metric


def _find_dimensions(question: str, registry: SemanticRegistry) -> list[str]:
    """Return all matching dimension names.

    Two-pass approach:
    1. Compound phrase matches (keywords with spaces) are found first.
    2. Standalone single-word matches are added only if the word was NOT the
       trailing word of a compound match found in pass 1 — preventing e.g.
       "seller state" from also triggering customer_state via the word "state".
    """
    dimension_keywords = _build_dynamic_dimension_keywords(registry)
    matched: list[str] = []
    suppressed_words: set[str] = set()

    # If the user asked for a compound grouping phrase like "seller state" and
    # we don't have an exact semantic dimension for it, suppress falling back to
    # the generic tail word ("state"), which would otherwise misroute.
    stopword_heads = {"by", "per", "group", "grouped", "show"}
    for compound in re.finditer(r"\b([a-z_]+)\s+(state|type|status|category|score|rate|method|code|name|city|country|region)\b", question):
        if compound.group(1) not in stopword_heads:
            suppressed_words.add(compound.group(2))

    # Pass 1: compound phrases
    for dimension_name, keywords in dimension_keywords:
        for keyword in keywords:
            if " " in keyword and _contains_phrase(question, keyword):
                if dimension_name not in matched:
                    matched.append(dimension_name)
                # Suppress the last word of the matched compound
                last_word = keyword.split()[-1]
                suppressed_words.add(last_word)
                break

    # Pass 2: single-word keywords not suppressed by pass 1
    for dimension_name, keywords in dimension_keywords:
        if dimension_name in matched:
            continue
        for keyword in keywords:
            if " " not in keyword and keyword not in suppressed_words:
                if _contains_phrase(question, keyword):
                    matched.append(dimension_name)
                    break

    return matched


MAX_RANKED_ROWS = 100
_TOP_N = re.compile(r"\b(top|bottom|first|best|worst|highest|lowest|largest|smallest|biggest|which)\s+(\d{1,3})\b"
                    r"(?!\s*(day|week|month|quarter|year)s?\b)")
_ASCENDING = re.compile(r"\b(fewest|least|lowest|smallest|bottom)\b")
# "Which is the top selling album?" asks for one album, by name (Chinook: it listed 100).
_THE_ONE = re.compile(r"\b(which|what|who)\s+(is|was|'s)\s+the\s+(top|best|most|highest|largest|biggest|"
                      r"lowest|least|worst|smallest|fewest|bottom)\b")


@dataclass(frozen=True)
class RankingRequest:
    limit: int | None  # rows asked for ("top 5", "which is the top..." = 1), None when not said
    ascending: bool    # "fewest", "lowest", "bottom"; "first N" too, as the golden set pins it (not "first quarter")


def ranking_request(question: str) -> RankingRequest:
    """What a question says about how many rows and which end: the one reading both the keyword
    heuristic and the LLM path's check use (they had different words and caps, 500 and 100)."""
    lowered = question.lower()
    match = _TOP_N.search(lowered)
    if match and 1 <= int(match.group(2)) <= MAX_RANKED_ROWS:
        limit = int(match.group(2))
    elif not match and _THE_ONE.search(lowered):
        limit = 1
    else:
        limit = None
    first_n = bool(match) and match.group(1) == "first"
    return RankingRequest(limit=limit, ascending=bool(_ASCENDING.search(lowered)) or first_n)


def _find_time_range(question: str) -> tuple[str | None, str | None]:
    year_match = re.search(r"\b(201[5-9]|202[0-9])\b", question)
    if year_match:
        year = year_match.group(1)
        return f"{year}-01-01", f"{year}-12-31"
    return None, None


def _detect_time_granularity(question: str) -> tuple[str | None, bool]:
    if any(token in question for token in ("by week", "weekly", "week by week", "per week")):
        return "week", True
    if any(token in question for token in ("by quarter", "quarterly", "per quarter")):
        return "quarter", True
    if any(token in question for token in ("by year", "yearly", "annual", "annually", "per year")):
        return "year", True
    if any(token in question for token in ("by day", "daily", "per day", "day by day")):
        return "day", True
    if any(token in question for token in ("over time", "monthly", "trend", "by month", "per month", "each month")):
        return "month", True
    return None, False


class HeuristicIntentMapper:
    def map(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> SemanticIntent:
        q = question.lower()

        metric = _find_metric(q, registry)
        if metric is None:
            raise IntentMappingError(
                "Question could not be grounded in the active schema; no matching metric was found"
            )
        dimensions = _find_dimensions(q, registry)

        # Post-filter: remove dimensions whose name phrase is subsumed by the
        # selected metric name (e.g. "review_score" dim when metric is
        # "average_review_score" — the user is asking about the metric, not
        # grouping by that dimension).
        metric_phrase = metric.replace("_", " ")
        dimensions = [
            d for d in dimensions
            if d.replace("_", " ") not in metric_phrase
        ]

        time_granularity, has_time_trend = _detect_time_granularity(q)

        # Use the registry's first time dimension instead of hardcoded "order_date"
        default_time_dim = (
            registry.schema.time_dimensions[0].name
            if registry.schema.time_dimensions
            else None
        )
        # Only set time_dimension when there is actually a time dimension available;
        # otherwise fall back to using year/date as a regular grouping dimension.
        time_dimension = default_time_dim if (has_time_trend and default_time_dim) else None
        order_by: str = "time_asc" if (has_time_trend and default_time_dim) else "metric_desc"

        # If user asked for a time trend but no time_dimension is in the schema,
        # try to find a year/date dimension and add it as a regular grouping dimension.
        if has_time_trend and not default_time_dim:
            year_dims = [
                d.name for d in registry.schema.dimensions
                if any(kw in d.name.lower() for kw in ("year", "date", "month", "period"))
            ]
            for yd in year_dims:
                if yd not in dimensions:
                    dimensions.append(yd)
                    break

        ranking = ranking_request(q)
        if not has_time_trend and ranking.ascending:
            order_by = "metric_asc"

        start_date, end_date = _find_time_range(q)
        limit = ranking.limit if ranking.limit is not None else 100

        if not has_time_trend and not dimensions and any(
            marker in q for marker in (" by ", " per ", "group by", "grouped by", "breakdown by")
        ):
            raise IntentMappingError(
                "Question references a grouping that could not be grounded in the active schema"
            )

        return SemanticIntent(
            metric=metric,
            dimensions=dimensions,
            time_dimension=time_dimension,
            time_granularity=time_granularity,
            order_by=order_by,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
        )


def _build_prompt_context(registry: SemanticRegistry, schema: SchemaContext) -> str:
    context = {
        "metrics": [{"name": metric.name, "description": metric.description} for metric in registry.schema.metrics],
        "dimensions": [
            {
                "name": dimension.name,
                "display_name": dimension.display_name,
                "tables": [_parse_table_ref(table_ref) for table_ref in dimension.required_tables],
            }
            for dimension in registry.schema.dimensions
        ],
        "time_dimensions": [
            {
                "name": time_dimension.name,
                "table": time_dimension.table,
                "default_granularity": time_dimension.default_granularity,
            }
            for time_dimension in registry.schema.time_dimensions
        ],
        "tables": sorted(_schema_table_names(registry, schema)),
        # Conditions only, never breakdowns: "tracks longer than 5 minutes", "rentals not returned".
        **({"column_filters": [_column_filter_entry(c) for c in registry.schema.filter_columns]}
           if registry.schema.filter_columns else {}),
        "order_by_options": ["metric_desc", "metric_asc", "time_asc", "time_desc"],
        "max_limit": 5000,
    }
    return json.dumps(context, separators=(",", ":"))


def _column_filter_entry(column) -> dict:
    entry = {"name": column.name, "display_name": column.display_name, "type": column.kind}
    if column.unit:
        entry["unit"] = column.unit
    return entry


def _build_system_prompt() -> str:
    return (
        "You map business questions to a SemanticIntent JSON object. "
        "Output JSON only. Do not include markdown fences or explanations. "
        "Choose only metrics, dimensions, time dimensions, and order_by values that exist in the provided registry. "
        "Use ISO-8601 date strings for start_date and end_date when present.\n\n"
        "CRITICAL RULES for time_dimension vs date filters:\n"
        "- time_dimension means GROUP BY a time period — it creates a time-series trend chart. "
        "Only set time_dimension when the user explicitly asks for a temporal trend: "
        "'over time', 'monthly trend', 'by month', 'weekly', 'daily', 'quarterly', 'by year'.\n"
        "- Date ranges like 'from 2010 to 2012', 'in 2018', 'last year', 'between X and Y' are "
        "time FILTERS — use start_date and end_date, NOT time_dimension.\n"
        "- If the question asks for a breakdown 'by category/state/type' WITH a date range, "
        "set dimensions + start_date/end_date. Do NOT set time_dimension.\n"
        "- Only set time_dimension when the user wants to see HOW a metric changes over time periods.\n\n"
        "Conditions on a column's value (registry 'column_filters'): put them in filters with that "
        "column's name, never in dimensions. Compare numbers in the column's unit (5 minutes = 300000 "
        "milliseconds; 1 MB = 1000000 bytes); 'negative' is lt 0; 'no'/'zero' is eq 0. 'Not ... yet', "
        "'never', 'missing' or 'without a' date is operator is_null on that date column (no value); "
        "'has a' date is is_not_null. A count of the things that meet the condition uses the count "
        "metric of those things."
    )


_ANSWERABILITY_PROMPT = (
    "You decide whether a question can be answered from a data model. The data model has only the metrics, "
    "dimensions, time dimensions and column conditions listed. Synonyms, paraphrases, filters on dimension "
    "values, conditions on a listed column (longer than, negative, not yet returned), date ranges, "
    "rankings and trends of these are all answerable. A question is NOT answerable only when it clearly needs "
    "a measure, breakdown or subject that none of them represents (for example a column the model does not "
    "have), or is not a question about this data at all. When in doubt, it is answerable. "
    "Every time dimension can be grouped by day, week, month, quarter or year and limited to any period, so "
    "'by year', 'per quarter' or 'in 1998' never makes a question unanswerable. A dimension's column says what "
    "it describes: nation.n_name is the nation's name, region.r_name the region's. "
    'Respond with JSON only: {"answerable": true} or {"answerable": false, "missing": "short phrase"}.'
)


def check_answerable(question: str, registry: SemanticRegistry, client: "LLMCompletionClient", model: str,
                     timeout_ms: int, previous_question: str | None = None) -> str | None:
    """What the data model is missing to answer the question, or None when it can answer it (or the
    check itself fails: it must never block a question)."""
    schema = registry.schema
    model_text = json.dumps({
        # With their tables and columns: on the benchmark's TPC-H layer "n_name (N Name)" read as
        # nothing, and "revenue by nation" / "by region" were refused as missing (ablation, 0d).
        "metrics": [f"{m.name} ({m.display_name}): {m.description} [{', '.join(m.required_tables)}]"
                    for m in schema.metrics],
        "dimensions": [f"{d.name} ({d.display_name}): {d.sql_expression[:80]}" for d in schema.dimensions],
        "time_dimensions": [f"{t.name} ({t.display_name}): {t.sql_expression[:80]}" for t in schema.time_dimensions],
        "time_grains": ["day", "week", "month", "quarter", "year"],
        **({"column_conditions": [f"{c.name} ({c.description or c.display_name})" for c in schema.filter_columns]}
           if schema.filter_columns else {}),
    }, ensure_ascii=False)
    try:
        raw = client.complete_json(system_prompt=_ANSWERABILITY_PROMPT,
                                   user_prompt=f"Data model: {model_text}\n"
                                   + (f"Previous question in this conversation: {previous_question}\n"
                                      "The new question may be a follow-up that only changes it (e.g. 'make it "
                                      "quarterly', 'only 2017'); judge it together with the previous question.\n"
                                      if previous_question else "")
                                   + f"Question: {question}",
                                   model=model, timeout_ms=timeout_ms)
        verdict = json.loads(_strip_json_fences(raw))
    except Exception as exc:
        if is_budget_exceeded(exc):
            raise
        logger.warning("Answerability check failed: %s", exc)
        return None
    if isinstance(verdict, dict) and verdict.get("answerable") is False:
        missing = str(verdict.get("missing") or "what this question needs").strip()[:120]
        if _names_registry_item(missing, registry):
            # The "missing" thing is in the model ("order_status" for "orders in transit"): the
            # check did not see that a value of an existing dimension was asked for. Answerable.
            logger.info("Answerability veto: %r names a registry item", missing)
            return None
        return missing
    return None


def _is_list_filter_value(missing: str, intent: SemanticIntent, registry: SemanticRegistry) -> bool:
    """Whether the "missing" thing is a value the intent filters a list dimension on: "restaurants
    that serve sweets" was refused ("no sweets") although cuisines holds Desserts, Mithai and Bakery;
    snapping matches such a word by meaning. Only list dimensions: elsewhere a refused value may be
    a real gap."""
    folded = re.sub(r"[^a-z0-9]+", " ", missing.lower()).strip()
    for condition in intent.filters:
        try:
            if not registry.get_dimension(condition.dimension).separator:
                continue
        except Exception:
            continue
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        for value in values:
            text = re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
            if text and (text in folded or folded in text):
                return True
    return False


_COVER_STOPWORDS = {"number", "count", "many", "much", "the", "and", "with", "from", "for", "per", "each", "all",
                    "how", "what", "which", "are", "is", "there", "in", "of", "by", "to", "on", "a", "an",
                    "total", "sum", "average", "mean", "top", "most", "least", "data", "our", "only",
                    "who", "whom", "whose", "was", "were", "has", "have", "had", "did", "does", "been", "they",
                    "their", "them", "its", "than", "that", "this", "these", "those", "give", "show", "list"}


def _word_forms(word: str) -> set[str]:
    forms = {word}
    if word.endswith("ies"):
        forms.add(word[:-3] + "y")
    elif word.endswith("es"):
        forms |= {word[:-2], word[:-1]}
    elif word.endswith("s"):
        forms.add(word[:-1])
    return forms


def _intent_covers(missing: str, intent: SemanticIntent, registry: SemanticRegistry) -> bool:
    """Whether every content word of the "missing" thing is named by the intent itself: its metric,
    dimensions, filtered dimensions or filter values. With 20 metrics and 20 dimensions (Chinook)
    the check refused "number of tracks by genre" while the mapping was track_count by genre_name.
    A word the intent does not have keeps the refusal: "average age" (no age), "profit by genre"
    (no profit), "tracks longer than 5 minutes" ("track length": no length)."""
    words = [w for w in re.findall(r"[a-z0-9]+", missing.lower()) if w not in _COVER_STOPWORDS and len(w) > 2]
    if not words:
        return False
    vocabulary: set[str] = set()
    try:
        metric = registry.get_metric(intent.metric)
        texts = [metric.name, metric.display_name]
        for name in [*intent.dimensions, *(c.dimension for c in intent.filters)]:
            dimension = registry.get_dimension(name)
            texts += [dimension.name, dimension.display_name, getattr(dimension, "description", None) or ""]
        for condition in intent.filters:
            texts += [str(v) for v in (condition.value if isinstance(condition.value, list) else [condition.value])]
    except Exception:
        return False
    for text in texts:
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vocabulary |= _word_forms(word)
    if vocabulary & _MONEY_METRIC_WORDS:
        # "Revenue", "sales", "paid" are what a payment amount or a total price measures (Pagila:
        # "what is the total revenue?" was refused while the mapping was total_payment_amount).
        vocabulary |= _MONEY_QUESTION_WORDS
    return all(_word_forms(word) & vocabulary for word in words)


_MONEY_METRIC_WORDS = {"amount", "payment", "price", "revenue", "sales", "value", "cost", "spend", "spent", "income"}
_MONEY_QUESTION_WORDS = {"revenue", "sales", "sale", "income", "earnings", "turnover", "takings", "money",
                         "paid", "pay", "pays", "spent", "spend", "spends", "spending", "sold", "earned",
                         "sell", "sells", "selling", "bestselling"}


def _names_list_item(missing: str, index, registry: SemanticRegistry | None = None) -> bool:
    """Whether a word (or two-word phrase) of the "missing" thing names an item of a list dimension,
    singular or plural: "This data has no bakeries" / "no juice bars" / "no number of restaurants
    that serve burgers", while cuisines holds Bakery, Juices and Burger. Or names one by meaning
    ("steakhouses" while cuisines holds Steak), at a higher bar than filters use."""
    dimensions = getattr(index, "list_dimensions", None)
    if not dimensions:
        return False
    words = re.findall(r"[a-z0-9]+", missing.lower())
    phrases = [p for p in words + [f"{a} {b}" for a, b in zip(words, words[1:])] if len(p) >= 3]
    if any(index.exact(dimension, phrase) for dimension in dimensions for phrase in phrases):
        return True
    # By meaning ("steakhouses" -> Steak 0.81, "coffee places" -> Cafe 0.84). Real gaps measured on
    # the restaurant cuisines stay lower ("revenue" -> BBQ 0.63, "menu prices" 0.59), hence the bar.
    similar = getattr(index, "similar", None)
    if similar is None:
        return False
    # Words of the data model's own names ("restaurants", "rating") say what is counted, not which item.
    own = set()
    if registry is not None:
        for item in (*registry.schema.metrics, *registry.schema.dimensions):
            own |= set(re.findall(r"[a-z0-9]+", f"{item.name} {item.display_name}".lower()))
    own |= {w[:-1] for w in own if w.endswith("s")} | {f"{w}s" for w in own}
    content = [w for w in words if len(w) >= 4 and w not in _VETO_STOPWORDS and w not in own]
    candidates = list(dict.fromkeys([f"{a} {b}" for a, b in zip(content, content[1:])] + content))[:MEANING_VETO_PHRASES]
    for dimension in dimensions:
        for phrase in candidates:
            matches = similar(dimension, phrase)
            if matches and matches[0][1] >= MEANING_VETO_MIN:
                return True
    return False


MEANING_VETO_MIN = 0.75
MEANING_VETO_PHRASES = 8
_VETO_STOPWORDS = {"number", "count", "average", "total", "many", "much", "that", "with", "which", "what", "this",
                   "have", "there", "data", "serve", "serves", "serving", "place", "places", "offer", "offering"}


def _names_registry_item(phrase: str, registry: SemanticRegistry) -> bool:
    """Whether the phrase contains a whole metric/dimension name or label ("payment_type",
    "Payment Method"), not just a shared word ("product weight" vs "Product Category")."""
    folded = f" {re.sub(r'[^a-z0-9]+', ' ', phrase.lower())} "
    schema = registry.schema
    for item in (*schema.metrics, *schema.dimensions, *schema.time_dimensions):
        for name in (item.name, item.display_name):
            words = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            if words and f" {words} " in folded:
                return True
    return False


def _build_dynamic_examples(registry: SemanticRegistry) -> list[dict]:
    """Generate few-shot examples dynamically from the schema's metrics/dimensions."""
    examples: list[dict] = []
    metrics = registry.schema.metrics
    dims = registry.schema.dimensions
    time_dims = registry.schema.time_dimensions

    if not metrics:
        return examples

    m0 = metrics[0]
    d0 = dims[0] if dims else None
    td0 = time_dims[0] if time_dims else None

    # Example 1: breakdown (metric by dimension)
    if d0:
        examples.append({
            "question": f"Top 10 {d0.display_name} by {m0.display_name}",
            "intent": {
                "metric": m0.name,
                "dimensions": [d0.name],
                "filters": [],
                "time_dimension": None,
                "time_granularity": None,
                "start_date": None,
                "end_date": None,
                "order_by": "metric_desc",
                "limit": 10,
            },
        })

    # Example 2: trend (metric over time)
    if td0:
        examples.append({
            "question": f"{td0.default_granularity}ly {m0.display_name} over time",
            "intent": {
                "metric": m0.name,
                "dimensions": [],
                "filters": [],
                "time_dimension": td0.name,
                "time_granularity": td0.default_granularity,
                "start_date": None,
                "end_date": None,
                "order_by": "time_asc",
                "limit": 100,
            },
        })

    # Example 3: scalar (just a metric)
    m_scalar = metrics[1] if len(metrics) > 1 else m0
    examples.append({
        "question": f"What is the {m_scalar.display_name}?",
        "intent": {
            "metric": m_scalar.name,
            "dimensions": [],
            "filters": [],
            "time_dimension": None,
            "time_granularity": None,
            "start_date": None,
            "end_date": None,
            "order_by": "metric_desc",
            "limit": 1,
        },
    })

    # Example 4: categorical breakdown with date filter (NOT time_dimension)
    if d0:
        examples.append({
            "question": f"{m0.display_name} by {d0.display_name} in 2018",
            "intent": {
                "metric": m0.name,
                "dimensions": [d0.name],
                "filters": [],
                "time_dimension": None,
                "time_granularity": None,
                "start_date": "2018-01-01",
                "end_date": "2018-12-31",
                "order_by": "metric_desc",
                "limit": 100,
            },
        })

    # Example 5: time-series trend broken down by dimension
    if td0 and d0:
        examples.append({
            "question": f"Monthly {m0.display_name} by {d0.display_name} over time",
            "intent": {
                "metric": m0.name,
                "dimensions": [d0.name],
                "filters": [],
                "time_dimension": td0.name,
                "time_granularity": "month",
                "start_date": None,
                "end_date": None,
                "order_by": "time_asc",
                "limit": 100,
            },
        })

    return examples


def _build_user_prompt(
    question: str,
    registry: SemanticRegistry,
    schema: SchemaContext,
    value_hints: list[dict] | None = None,
    context: str | None = None,
    examples: list[dict] | None = None,
) -> str:
    # Retrieved per question when the connection's examples index is ready (Phase 3d); the static
    # examples otherwise.
    examples = examples or _build_dynamic_examples(registry)
    hints = ""
    if value_hints:
        hints = (
            "Values found in the data that may match words in the question (use the exact 'value' "
            "in filters, only if the question really refers to it): "
            f"{json.dumps([{'dimension': h['dimension'], 'value': h['value']} for h in value_hints], ensure_ascii=False, separators=(',', ':'))}\n"
        )
    return (
        f"Semantic registry: {_build_prompt_context(registry, schema)}\n"
        f"Examples: {json.dumps(examples, separators=(',', ':'))}\n"
        f"{hints}"
        f"{context.strip() + chr(10) if context else ''}"
        f"Question: {question}\n"
        "Return only a JSON object matching the SemanticIntent schema."
    )


def _extract_intent_with_instructor(
    *,
    provider: Literal["anthropic", "openai", "bedrock"] | None,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_ms: int,
) -> SemanticIntent:
    # One attempt: the agent's repair loop asks again with the reason when an intent is invalid.
    # Instructor's own retries resent the identical request: qwen3:4b answers in text rather than
    # a tool call, and each question made three identical mapping calls before the text was
    # recovered (seen in the per-call spend log).
    return structured_completion(provider=provider, model=model, system_prompt=system_prompt,
                                 user_prompt=user_prompt, response_model=SemanticIntent, timeout_ms=timeout_ms,
                                 max_retries=1)


def _extract_intent_from_json_client(
    *,
    client: LLMCompletionClient,
    system_prompt: str,
    user_prompt: str,
    model: str,
    timeout_ms: int,
) -> SemanticIntent:
    raw = client.complete_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        timeout_ms=timeout_ms,
    )
    try:
        payload = json.loads(_strip_json_fences(raw))
    except json.JSONDecodeError as exc:
        raise IntentMappingError(f"LLM output was not valid JSON: {exc}") from exc

    # Examples are shown as {"question": ..., "intent": {...}}; small models often answer in that
    # shape. Unwrap it rather than rejecting a correct intent.
    if isinstance(payload, dict) and "metric" not in payload and isinstance(payload.get("intent"), dict):
        payload = payload["intent"]
    try:
        return SemanticIntent.model_validate(payload)
    except ValidationError as exc:
        raise IntentMappingError(f"LLM output did not match SemanticIntent schema: {exc}") from exc


class LLMIntentMapper:
    def __init__(
        self,
        *,
        config: IntentMapperConfig,
        client: LLMCompletionClient | None = None,
    ) -> None:
        self._config = config.for_task("intent")
        self._client = client or build_completion_client(self._config.provider)

    def map(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
        value_hints: list[dict] | None = None,
        context: str | None = None,
        examples: list[dict] | None = None,
    ) -> SemanticIntent:
        if not self._config.model:
            raise IntentMappingError("LLM_MODEL must be set when INTENT_MODE=llm")
        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(question, registry, schema, value_hints, context, examples)
        primary_error: Exception | None = None

        try:
            return _extract_intent_with_instructor(
                provider=self._config.provider,
                model=self._config.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_ms=self._config.timeout_ms,
            )
        except Exception as exc:
            if is_budget_exceeded(exc):
                raise  # never another hosted call (the JSON fallback below) past the spending cap
            primary_error = exc

        try:
            return _extract_intent_from_json_client(
                client=self._client,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self._config.model,
                timeout_ms=self._config.timeout_ms,
            )
        except Exception as exc:
            if primary_error is None:
                raise IntentMappingError(str(exc)) from exc
            raise IntentMappingError(
                f"Instructor mapping failed ({primary_error}); JSON fallback failed ({exc})"
            ) from exc


_ANSWERABILITY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="answerability")


_PREVIOUS = re.compile(r"Previous question in this conversation: (.*)")


def _previous_question(context: str | None) -> str | None:
    """The previous turn's question, from the context the agent gives the mapper."""
    match = _PREVIOUS.search(context or "")
    return match.group(1).strip() if match else None


_PREVIOUS_METRIC = re.compile(r'Its intent: \{[^\n]*?"metric":\s*"([A-Za-z0-9_]+)"')


def _previous_metric(context: str | None) -> str | None:
    match = _PREVIOUS_METRIC.search(context or "")
    return match.group(1) if match else None


def context_is_repair(context: str | None) -> bool:
    return bool(context) and "your previous answer" in context.lower()  # both repair messages say this


class IntentMapperRouter:
    def __init__(
        self,
        *,
        config: IntentMapperConfig,
        heuristic_mapper: IntentMapper | None = None,
        llm_mapper: IntentMapper | None = None,
        value_index: Callable[[], "ValueIndex | None"] | None = None,
        example_index: Callable[[], "ExampleIndex | None"] | None = None,
        answerability: Callable[..., str | None] | None = None,
    ) -> None:
        self._config = config
        # (question, registry) -> what is missing, or None. Default: an LLM check in llm mode.
        self._answerability = answerability
        # Phase 3d: returns the connection's examples index once it is built, else None.
        self._example_index = example_index or (lambda: None)
        # Grounding (Phase 2): returns the connection's value index once it is built, else None.
        self._value_index = value_index or (lambda: None)
        self._heuristic_mapper = heuristic_mapper or HeuristicIntentMapper()
        self._llm_init_error: IntentMappingError | None = None
        if llm_mapper is not None:
            self._llm_mapper = llm_mapper
        elif config.mode == "llm":
            try:
                self._llm_mapper = LLMIntentMapper(config=config)
                task = config.for_task("answerability")
                if answerability is None and task.model:
                    client = build_completion_client(task.provider)
                    self._answerability = lambda q, reg, prev=None: check_answerable(
                        q, reg, client, task.model, task.timeout_ms, previous_question=prev)
            except LLMError as exc:
                self._llm_mapper = None
                self._llm_init_error = exc if isinstance(exc, IntentMappingError) else IntentMappingError(str(exc))
        else:
            self._llm_mapper = None

    @property
    def config(self) -> IntentMapperConfig:
        return self._config

    @property
    def uses_llm(self) -> bool:
        return self._config.mode == "llm"

    def value_index(self):
        """The connection's value index when it is built, else None."""
        return self._current_index()

    def propose(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
        *,
        context: str | None = None,
        cache: dict | None = None,
    ) -> IntentProposal:
        """One unvalidated mapping attempt. The agent graph validates it and decides whether to
        repair (ask the LLM again with the error), clarify, or fall back to the heuristic mapper.
        `cache` holds the value hints and examples already looked up for this question: a repair
        round asks the same question again, so it reuses them instead of searching again."""
        if not self.uses_llm:
            intent = self._heuristic_mapper.map(question, registry, schema)
            return IntentProposal(intent=intent, source="heuristic", error=None, trace=["Intent mapper: source=heuristic"])
        hints_started = time.perf_counter()
        hints = cache["hints"] if cache is not None else self._value_hints(question)
        hints_ms = round((time.perf_counter() - hints_started) * 1000, 2)
        examples_started = time.perf_counter()
        if cache is not None:
            examples = cache["examples"]
        else:
            found = self._examples(question)
            examples = [{"question": e.question, "intent": e.intent} for e in found] if found else None
        examples_ms = round((time.perf_counter() - examples_started) * 1000, 2)
        started_at = time.perf_counter()
        # The answerability check runs beside the mapping (a separate, focused call: adding the rule to
        # the mapping prompt made the 4B model's intents worse). Not on repairs: the question is the same.
        verdict = None
        format_retry = False
        if self._answerability is not None and not context_is_repair(context):
            # In this question's context, so its tokens count toward the question (llm.usage_meter).
            verdict = _ANSWERABILITY_POOL.submit(contextvars.copy_context().run, self._answerability, question,
                                                 registry, _previous_question(context))
        try:
            if self._llm_mapper is None:
                raise self._llm_init_error or IntentMappingError("LLM mapper is not configured")
            kwargs = {}
            if hints:
                kwargs["value_hints"] = hints
            if context:
                kwargs["context"] = context
            if examples:
                kwargs["examples"] = examples
            try:
                intent = self._llm_mapper.map(question, registry, schema, **kwargs)
            except Exception as exc:
                if is_budget_exceeded(exc) or not any(marker in str(exc) for marker in _FORMAT_FAILURES):
                    raise
                format_retry = True
                intent = self._llm_mapper.map(question, registry, schema, **kwargs)
            error = None
        except Exception as exc:
            if is_budget_exceeded(exc):
                raise  # not a mapping error to repair: the repair would call the model again
            intent, error = None, str(exc)
        missing = verdict.result() if verdict is not None else None
        if missing and intent is not None and intent.metric == _previous_metric(context):
            # A follow-up that keeps the previous turn's metric refines an answerable question
            # ("Show the worst 5"); the check misreads such elliptical questions.
            missing = None
        if missing and intent is not None and _is_list_filter_value(missing, intent, registry):
            missing = None
        if missing and intent is not None and _intent_covers(missing, intent, registry):
            missing = None
        if missing and _names_list_item(missing, self._current_index(), registry):
            # Even when the mapping failed ("no juice bars"): the repair loop retries the mapping.
            missing = None
        if missing and intent is not None:
            intent = intent.model_copy(update={"unsupported": missing})
        elif missing:
            intent, error = SemanticIntent(metric="unknown", unsupported=missing), None
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        reused = " (reused)" if cache is not None else ""
        answerability = ("not answerable: " + missing if missing else
                         "ok" if verdict is not None else "skipped (repair: same question)"
                         if self._answerability is not None else "off")
        trace = [f"Intent mapper: value hints {len(hints or [])} in {hints_ms} ms{reused}",
                 f"Intent mapper: answerability {answerability}",
                 "Intent mapper: " + (f"examples retrieved{reused} in {examples_ms} ms: "
                                      + " | ".join(e["question"] for e in examples) if examples else "static examples"),
                 f"Intent mapper: source=llm latency_ms={latency_ms}"
                 + (" (with context)" if context else "") + (" (format retry)" if format_retry else "")
                 + (f" error={error[:300]}" if error else "")]
        return IntentProposal(intent=intent, source="llm", error=error, trace=trace, value_hints=hints,
                              examples=examples)

    def fallback(self, question: str, registry: SemanticRegistry, schema: SchemaContext, reason: str) -> IntentMappingResult:
        """The heuristic answer after the LLM could not produce a valid intent."""
        self._log_llm_failure(question, 0.0, reason)
        if not self._config.fallback_to_heuristic:
            raise IntentMappingError(f"LLM intent mapping failed: {reason}")
        try:
            result = self._map_heuristic(question, registry, schema)
        except IntentValidationError as exc:
            # No valid reading at all: "rephrase" (422), not "unsafe query" (400).
            raise IntentMappingError(f"No valid reading after the LLM failed: {exc}") from None
        return IntentMappingResult(intent=result.intent, source="llm_fallback",
                                   trace=[f"Intent mapper: source=llm_fallback reason={reason[:300]}", *result.trace[1:]])

    def _current_index(self):
        try:
            return self._value_index()
        except Exception:
            return None

    def _examples(self, question: str):
        try:
            index = self._example_index()
            return index.retrieve(question) if index is not None else None
        except Exception as exc:  # retrieval must never break a question
            logger.warning("Example retrieval failed: %s", exc)
            return None

    def _value_hints(self, question: str) -> list[dict] | None:
        index = self._current_index()
        if index is None:
            return None
        try:
            # "exact": the question names the value word for word (check_intent may then turn a breakdown into a filter).
            return [{"dimension": m.dimension, "value": m.value, "exact": m.exact} for m in index.candidates(question)]
        except Exception as exc:
            logger.warning("Value grounding failed: %s", exc)
            return None

    def _map_heuristic(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> IntentMappingResult:
        intent = self._heuristic_mapper.map(question, registry, schema)
        # The keyword heuristic can match 3-4 dimensions on a wide file ("How many restaurants have
        # an Excellent rating?" on Zomato, after the LLM failed): keep the ones the question names,
        # up to the cap, instead of failing as an "unsafe query".
        cap = 1 if intent.time_dimension else 2
        if len(intent.dimensions) > cap:
            lowered = question.lower()
            ranked = sorted(intent.dimensions, key=lambda d: not any(
                w in lowered for w in d.split("_") if len(w) > 3))
            intent = intent.model_copy(update={"dimensions": ranked[:cap]})
        validated = validate_semantic_intent(intent, registry, schema)
        return IntentMappingResult(
            intent=validated,
            source="heuristic",
            trace=[
                "Intent mapper: source=heuristic",
                f"Intent mapper: selected metric '{validated.metric}'",
                "Intent mapper: resolved dimensions "
                + (", ".join(validated.dimensions) if validated.dimensions else "none"),
            ],
        )

    def _log_llm_failure(self, question: str, latency_ms: float, reason: str) -> None:
        extra = {
            "mode": self._config.mode,
            "provider": self._config.provider,
            "model": self._config.model,
            "latency_ms": latency_ms,
            "reason": reason,
        }
        if self._config.debug_logging:
            extra["question"] = question
        logger.warning("LLM intent mapping failed", extra=extra)
