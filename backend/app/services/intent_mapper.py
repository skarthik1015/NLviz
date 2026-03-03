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
_EMPTY_SCHEMA = SchemaContext(tables={}, row_counts={}, join_paths=[])


class IntentMappingError(ValueError):
    pass


class IntentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class IntentMapperConfig:
    mode: Literal["heuristic", "llm"] = "heuristic"
    provider: Literal["anthropic", "openai", "bedrock"] | None = None
    model: str | None = None
    timeout_ms: int = 8000
    fallback_to_heuristic: bool = True
    debug_logging: bool = False

    @classmethod
    def from_env(cls) -> "IntentMapperConfig":
        mode = os.getenv("INTENT_MODE", "heuristic").strip().lower() or "heuristic"
        if mode not in {"heuristic", "llm"}:
            raise ValueError("INTENT_MODE must be one of: heuristic, llm")

        provider_value = os.getenv("LLM_PROVIDER", "").strip().lower() or None
        if provider_value and provider_value not in {"anthropic", "openai", "bedrock"}:
            raise ValueError("LLM_PROVIDER must be one of: anthropic, openai, bedrock")

        return cls(
            mode=mode,
            provider=provider_value,  # type: ignore[arg-type]
            model=os.getenv("LLM_MODEL", "").strip() or None,
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

        missing_tables = sorted(
            _parse_table_ref(table_ref)
            for table_ref in dimension.required_tables
            if _parse_table_ref(table_ref) not in available_tables
        )
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

        missing_tables = sorted(
            _parse_table_ref(table_ref)
            for table_ref in filter_dimension.required_tables
            if _parse_table_ref(table_ref) not in available_tables
        )
        if missing_tables:
            errors.append(
                f"Filter dimension '{filter_condition.dimension}' depends on unknown tables: {', '.join(missing_tables)}"
            )

    if metric is not None:
        missing_tables = sorted(
            _parse_table_ref(table_ref)
            for table_ref in metric.required_tables
            if _parse_table_ref(table_ref) not in available_tables
        )
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


_METRIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("average_order_value", ("average order value", "aov", "avg order value", "mean order value")),
    (
        "average_review_score",
        ("review score", "average review", "avg review", "rating", "ratings", "customer satisfaction"),
    ),
    (
        "average_delivery_days",
        ("delivery time", "delivery days", "shipping time", "how long to deliver", "days to deliver"),
    ),
    ("cancellation_rate", ("cancellation rate", "cancel rate", "cancellation", "cancelled orders", "cancellations")),
    ("total_revenue", ("revenue", "sales", "gmv", "total sales", "income", "earnings")),
    ("order_count", ("order count", "number of orders", "orders placed", "how many orders", "orders")),
)

_DIMENSION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("seller_state", ("seller state", "by seller", "per seller location")),
    ("customer_state", ("customer state", "by state", "by region", "per state", "state", "region")),
    ("customer_id", ("by customer", "customer id")),
    (
        "product_category",
        ("product category", "by category", "per category", "category", "categories", "product type", "item type"),
    ),
    ("payment_type", ("payment method", "payment type", "by payment", "how paid")),
    ("order_status", ("order status", "by status", "status")),
    ("review_score", ("by review score", "by rating")),
)


def _contains_phrase(question: str, phrase: str) -> bool:
    escaped = re.escape(phrase)
    return re.search(rf"(?<!\w){escaped}(?!\w)", question) is not None


def _find_metric(question: str) -> str:
    for metric_name, keywords in _METRIC_KEYWORDS:
        if any(_contains_phrase(question, keyword) for keyword in keywords):
            return metric_name
    return "order_count"


def _find_dimensions(question: str) -> list[str]:
    dimensions: list[str] = []
    seller_state_matched = False

    for dimension_name, keywords in _DIMENSION_KEYWORDS:
        matched = any(_contains_phrase(question, keyword) for keyword in keywords)
        if not matched:
            continue

        if dimension_name == "customer_state" and seller_state_matched and not _contains_phrase(question, "customer state"):
            continue

        if dimension_name == "seller_state":
            seller_state_matched = True

        dimensions.append(dimension_name)

    if (
        re.search(r"\b(top|bottom|first)\s+\d+\s+customers?\b", question)
        and "customer_state" not in dimensions
        and "customer_id" not in dimensions
    ):
        dimensions.append("customer_id")

    return dimensions


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
        del registry, schema
        q = question.lower()

        metric = _find_metric(q)
        dimensions = _find_dimensions(q)
        time_granularity, has_time_trend = _detect_time_granularity(q)
        time_dimension = "order_date" if has_time_trend else None
        order_by: str = "time_asc" if has_time_trend else "metric_desc"

        rank_limit, rank_direction = _find_rank_limit(q)
        if not has_time_trend and rank_direction in {"bottom", "first"}:
            order_by = "metric_asc"

        start_date, end_date = _find_time_range(q)
        limit = rank_limit if rank_limit is not None else 100

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
        from anthropic import Anthropic

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
        from openai import OpenAI

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
        "Use ISO-8601 date strings for start_date and end_date when present."
    )


def _build_user_prompt(question: str, registry: SemanticRegistry, schema: SchemaContext) -> str:
    examples = [
        {
            "question": "top 10 customers by revenue in 2018",
            "intent": {
                "metric": "total_revenue",
                "dimensions": ["customer_id"],
                "filters": [],
                "time_dimension": None,
                "time_granularity": None,
                "start_date": "2018-01-01",
                "end_date": "2018-12-31",
                "order_by": "metric_desc",
                "limit": 10,
            },
        },
        {
            "question": "monthly revenue trend last 6 months",
            "intent": {
                "metric": "total_revenue",
                "dimensions": [],
                "filters": [],
                "time_dimension": "order_date",
                "time_granularity": "month",
                "start_date": None,
                "end_date": None,
                "order_by": "time_asc",
                "limit": 100,
            },
        },
        {
            "question": "orders count by state",
            "intent": {
                "metric": "order_count",
                "dimensions": ["customer_state"],
                "filters": [],
                "time_dimension": None,
                "time_granularity": None,
                "start_date": None,
                "end_date": None,
                "order_by": "metric_desc",
                "limit": 100,
            },
        },
    ]
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

        raw = self._client.complete_json(
            system_prompt=_build_system_prompt(),
            user_prompt=_build_user_prompt(question, registry, schema),
            model=self._config.model,
            timeout_ms=self._config.timeout_ms,
        )

        try:
            payload = json.loads(_strip_json_fences(raw))
        except json.JSONDecodeError as exc:
            raise IntentMappingError(f"LLM output was not valid JSON: {exc}") from exc

        try:
            return SemanticIntent.model_validate(payload)
        except ValidationError as exc:
            raise IntentMappingError(f"LLM output did not match SemanticIntent schema: {exc}") from exc


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


def map_question_to_intent(question: str) -> SemanticIntent:
    return HeuristicIntentMapper().map(question, registry=None, schema=_EMPTY_SCHEMA)  # type: ignore[arg-type]
