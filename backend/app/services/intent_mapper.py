from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ValidationError

from app.connectors.base import SchemaContext
from app.models.semantic_intent import SemanticIntent
from app.semantic import SemanticRegistry

logger = logging.getLogger(__name__)

IntentSource = Literal["heuristic", "llm", "llm_fallback", "explicit"]


class IntentMappingError(ValueError):
    pass


class IntentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class IntentMapperConfig:
    mode: Literal["heuristic", "llm"] = "llm"
    provider: Literal["anthropic", "openai", "bedrock"] | None = "anthropic"
    model: str | None = "claude-sonnet-4-5"
    timeout_ms: int = 8000
    fallback_to_heuristic: bool = True
    debug_logging: bool = False

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
        )


@dataclass(frozen=True)
class IntentMappingResult:
    intent: SemanticIntent
    source: IntentSource
    trace: list[str]


class IntentMapper(Protocol):
    def map(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> SemanticIntent:
        ...


class LLMCompletionClient(Protocol):
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout_ms: int,
    ) -> str:
        ...


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

    try:
        metric = registry.get_metric(intent.metric)
    except KeyError:
        errors.append(f"Unknown metric '{intent.metric}'")
        metric = None

    if len(intent.dimensions) != len(set(intent.dimensions)):
        errors.append("Dimensions must be unique")

    for dimension_name in intent.dimensions:
        try:
            dimension = registry.get_dimension(dimension_name)
        except KeyError:
            errors.append(f"Unknown dimension '{dimension_name}'")
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

    if errors:
        raise IntentValidationError("; ".join(errors))

    return intent


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


def _find_rank_limit(question: str) -> tuple[int | None, str | None]:
    match = re.search(r"\b(top|bottom|first)\s+(\d+)\b", question)
    if not match:
        return None, None
    return min(int(match.group(2)), 500), match.group(1)


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
        time_dimension = default_time_dim if has_time_trend else None
        order_by: str = "time_asc" if has_time_trend else "metric_desc"

        rank_limit, rank_direction = _find_rank_limit(q)
        if not has_time_trend and rank_direction in {"bottom", "first"}:
            order_by = "metric_asc"

        start_date, end_date = _find_time_range(q)
        limit = rank_limit if rank_limit is not None else 100

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


class AnthropicCompletionClient:
    def __init__(self) -> None:
        try:
            from anthropic import Anthropic
        except ModuleNotFoundError as exc:
            raise IntentMappingError(
                "Anthropic provider requires the 'anthropic' package: pip install anthropic"
            ) from exc
        self._client = Anthropic()

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout_ms: int,
    ) -> str:
        response = self._client.messages.create(
            model=model,
            max_tokens=800,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=timeout_ms / 1000,
        )
        return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


class OpenAICompletionClient:
    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise IntentMappingError(
                "OpenAI provider requires the 'openai' package: pip install openai"
            ) from exc
        self._client = OpenAI()

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout_ms: int,
    ) -> str:
        response = self._client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=timeout_ms / 1000,
        )
        content = response.choices[0].message.content
        if not content:
            raise IntentMappingError("OpenAI returned an empty response")
        return content


class BedrockCompletionClient:
    def __init__(self) -> None:
        try:
            import boto3
        except ModuleNotFoundError as exc:
            raise IntentMappingError(
                "Bedrock provider requires boto3 to be installed in the backend environment"
            ) from exc

        self._client = boto3.client("bedrock-runtime")

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout_ms: int,
    ) -> str:
        del timeout_ms
        response = self._client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"temperature": 0, "maxTokens": 800},
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block.get("text", "") for block in blocks if "text" in block)
        if not text:
            raise IntentMappingError("Bedrock returned an empty response")
        return text


def _build_completion_client(provider: Literal["anthropic", "openai", "bedrock"] | None) -> LLMCompletionClient:
    if provider == "anthropic":
        return AnthropicCompletionClient()
    if provider == "openai":
        return OpenAICompletionClient()
    if provider == "bedrock":
        return BedrockCompletionClient()
    raise IntentMappingError("LLM_PROVIDER must be set when INTENT_MODE=llm")


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
        "order_by_options": ["metric_desc", "metric_asc", "time_asc", "time_desc"],
        "max_limit": 5000,
    }
    return json.dumps(context, separators=(",", ":"))


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
        "- Only set time_dimension when the user wants to see HOW a metric changes over time periods."
    )


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


def _build_user_prompt(question: str, registry: SemanticRegistry, schema: SchemaContext) -> str:
    examples = _build_dynamic_examples(registry)
    return (
        f"Semantic registry: {_build_prompt_context(registry, schema)}\n"
        f"Examples: {json.dumps(examples, separators=(',', ':'))}\n"
        f"Question: {question}\n"
        "Return only a JSON object matching the SemanticIntent schema."
    )


def _strip_json_fences(payload: str) -> str:
    candidate = payload.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    return candidate


def _extract_intent_with_instructor(
    *,
    provider: Literal["anthropic", "openai", "bedrock"] | None,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_ms: int,
) -> SemanticIntent:
    try:
        import instructor
    except ModuleNotFoundError as exc:
        raise IntentMappingError("Instructor is required for LLM-first intent mapping") from exc

    if provider == "openai":
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise IntentMappingError("OpenAI provider requires the 'openai' package") from exc
        client = instructor.from_openai(OpenAI())
        return client.chat.completions.create(
            model=model,
            temperature=0,
            response_model=SemanticIntent,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=timeout_ms / 1000,
        )

    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ModuleNotFoundError as exc:
            raise IntentMappingError("Anthropic provider requires the 'anthropic' package") from exc
        client = instructor.from_anthropic(Anthropic())
        return client.messages.create(
            model=model,
            max_tokens=800,
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            response_model=SemanticIntent,
            timeout=timeout_ms / 1000,
        )

    raise IntentMappingError("Instructor intent mapping is not configured for this provider")


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
        self._config = config
        self._client = client or _build_completion_client(config.provider)

    def map(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> SemanticIntent:
        if not self._config.model:
            raise IntentMappingError("LLM_MODEL must be set when INTENT_MODE=llm")
        system_prompt = _build_system_prompt()
        user_prompt = _build_user_prompt(question, registry, schema)
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


class IntentMapperRouter:
    def __init__(
        self,
        *,
        config: IntentMapperConfig,
        heuristic_mapper: IntentMapper | None = None,
        llm_mapper: IntentMapper | None = None,
    ) -> None:
        self._config = config
        self._heuristic_mapper = heuristic_mapper or HeuristicIntentMapper()
        self._llm_init_error: IntentMappingError | None = None
        if llm_mapper is not None:
            self._llm_mapper = llm_mapper
        elif config.mode == "llm":
            try:
                self._llm_mapper = LLMIntentMapper(config=config)
            except IntentMappingError as exc:
                self._llm_mapper = None
                self._llm_init_error = exc
        else:
            self._llm_mapper = None

    def map(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> SemanticIntent:
        return self.map_with_metadata(question, registry, schema).intent

    def map_with_metadata(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> IntentMappingResult:
        if self._config.mode == "heuristic":
            return self._map_heuristic(question, registry, schema)

        started_at = time.perf_counter()
        try:
            if self._llm_mapper is None:
                if self._llm_init_error is not None:
                    raise self._llm_init_error
                raise IntentMappingError("LLM mapper is not configured")
            llm_intent = self._llm_mapper.map(question, registry, schema)
            validated = validate_semantic_intent(llm_intent, registry, schema)
            latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            self._log_llm_success(question, latency_ms)
            return IntentMappingResult(
                intent=validated,
                source="llm",
                trace=[
                    f"Intent mapper: source=llm latency_ms={latency_ms}",
                    f"Intent mapper: selected metric '{validated.metric}'",
                    "Intent mapper: resolved dimensions "
                    + (", ".join(validated.dimensions) if validated.dimensions else "none"),
                ],
            )
        except Exception as exc:
            mapped_error = exc if isinstance(exc, (IntentMappingError, IntentValidationError)) else IntentMappingError(str(exc))
            latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            self._log_llm_failure(question, latency_ms, str(mapped_error))
            if not self._config.fallback_to_heuristic:
                raise IntentMappingError(f"LLM intent mapping failed: {mapped_error}") from exc

            heuristic_result = self._map_heuristic(question, registry, schema)
            return IntentMappingResult(
                intent=heuristic_result.intent,
                source="llm_fallback",
                trace=[
                    f"Intent mapper: source=llm_fallback reason={mapped_error}",
                    f"Intent mapper: llm_latency_ms={latency_ms}",
                    f"Intent mapper: selected metric '{heuristic_result.intent.metric}'",
                    "Intent mapper: resolved dimensions "
                    + (
                        ", ".join(heuristic_result.intent.dimensions)
                        if heuristic_result.intent.dimensions
                        else "none"
                    ),
                ],
            )

    def _map_heuristic(
        self,
        question: str,
        registry: SemanticRegistry,
        schema: SchemaContext,
    ) -> IntentMappingResult:
        intent = self._heuristic_mapper.map(question, registry, schema)
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

    def _log_llm_success(self, question: str, latency_ms: float) -> None:
        extra = {
            "mode": self._config.mode,
            "provider": self._config.provider,
            "model": self._config.model,
            "latency_ms": latency_ms,
        }
        if self._config.debug_logging:
            extra["question"] = question
        logger.info("LLM intent mapping succeeded", extra=extra)

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
