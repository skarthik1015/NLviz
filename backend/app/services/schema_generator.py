"""LLM-powered semantic schema generation from physical DB introspection."""
from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from app.connectors.base import DataConnector, SchemaContext
from app.models.connection import GenerationMetadata, ValidationSummary
from app.semantic.models import SemanticSchema
from app.services.intent_mapper import (
    IntentMapperConfig,
    _build_completion_client,
    _strip_json_fences,
)

if TYPE_CHECKING:
    from app.storage.schema_storage import BaseSchemaStorage

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "data" / "schemas"

_SYSTEM_PROMPT = """\
You are a data analyst building a semantic layer for a business database.
Given the physical schema below, generate a SemanticSchema JSON object.

Rules:
- Generate 3-10 metrics representing the most useful business measures.
- For numeric columns: propose SUM and/or AVG metrics where meaningful.
- For ID columns on primary entities: propose COUNT_DISTINCT metrics.
- For status/flag columns with clear good/bad states: consider RATIO metrics.
- Dimensions: pick categorical columns with distinct value count < 500. Assign cardinality: low (< 20), medium (< 500), high (>= 500).
- Time dimensions: ONLY pick columns with actual DATE or TIMESTAMP data types. Do NOT use integer year columns (INT/BIGINT named 'year') as time dimensions — those belong as regular dimensions instead. Set default_granularity to "month".
- Joins: use the provided FK/inferred relationships. Default type = "LEFT".
- sql_expression: reference actual column names EXACTLY as they appear in the schema. Use table aliases in required_tables (e.g. "payments p") and reference via alias (e.g. "p.amount").
- required_tables: list ALL tables needed for the metric/dimension, including join intermediaries.
- base_filter: only add for clear data quality reasons (e.g. exclude NULLs in AVG columns).
- description: write clear, business-friendly descriptions.
- display_name: humanize column names (snake_case to Title Case).
- dataset: infer a short descriptive name from the table names.
- version: "1.0"

Output ONLY valid JSON matching the SemanticSchema structure. No markdown fences."""


def generate_semantic_schema(
    connector: DataConnector,
    connection_id: str,
    version_id: str,
    config: IntentMapperConfig | None = None,
    schema_storage: "BaseSchemaStorage | None" = None,
) -> tuple[SemanticSchema, str, ValidationSummary, GenerationMetadata]:
    """Generate a semantic schema from a database connector.

    Returns (schema, yaml_path, validation_summary, generation_metadata).
    """
    if config is None:
        config = IntentMapperConfig.from_env()

    start_ms = time.perf_counter()

    # Step 1: Introspect physical schema
    schema_ctx = connector.get_schema()
    physical_summary = _build_physical_summary(schema_ctx)
    table_count = len(schema_ctx.tables)
    column_count = sum(len(cols) for cols in schema_ctx.tables.values())

    # Step 2: LLM call to generate SemanticSchema
    semantic_schema = _generate_via_llm(physical_summary, config)

    generation_time_ms = round((time.perf_counter() - start_ms) * 1000, 2)

    # Step 3: Validate via execution probes
    from app.services.schema_validator import validate_schema

    validation = validate_schema(semantic_schema, connector, schema_ctx)

    # Step 4: Clean broken items & persist
    cleaned = _remove_broken_items(semantic_schema, validation)

    # Recompute validation after cleanup
    clean_validation = ValidationSummary(
        total_metrics=validation.total_metrics,
        valid_metrics=validation.valid_metrics,
        broken_metrics=validation.broken_metrics,
        total_dimensions=validation.total_dimensions,
        valid_dimensions=validation.valid_dimensions,
        broken_dimensions=validation.broken_dimensions,
        confidence_score=validation.confidence_score,
    )

    yaml_path = _persist_schema(cleaned, connection_id, version_id, schema_storage)

    metadata = GenerationMetadata(
        llm_provider=config.provider or "unknown",
        llm_model=config.model or "unknown",
        generation_time_ms=generation_time_ms,
        table_count=table_count,
        column_count=column_count,
    )

    return cleaned, yaml_path, clean_validation, metadata


def _build_physical_summary(ctx: SchemaContext) -> dict[str, Any]:
    """Build a token-budgeted summary of the physical schema for the LLM."""
    tables_summary: list[dict] = []
    total_columns = 0

    for table_name, columns in ctx.tables.items():
        total_columns += len(columns)

    # Token budget: if > 80 columns, truncate to top 5 per table
    truncate = total_columns > 80

    for table_name, columns in ctx.tables.items():
        col_list = columns
        if truncate:
            col_list = _prioritize_columns(columns, ctx.distinct_counts.get(table_name, {}))[:5]

        cols_info = []
        for col in col_list:
            col_info: dict[str, Any] = {
                "name": col["name"],
                "type": col["type"],
            }
            ndv = ctx.distinct_counts.get(table_name, {}).get(col["name"])
            if ndv is not None and ndv >= 0:
                col_info["distinct_count"] = ndv
            samples = col.get("sample_values", [])
            if samples:
                # Stringify samples for JSON safety
                col_info["sample_values"] = [str(s) for s in samples[:3]]
            cols_info.append(col_info)

        tables_summary.append({
            "name": table_name,
            "row_count": ctx.row_counts.get(table_name, 0),
            "columns": cols_info,
        })

    # Combine FK joins and heuristic joins
    all_joins = list(ctx.join_paths) + list(ctx.inferred_joins)

    return {
        "tables": tables_summary,
        "inferred_joins": all_joins,
    }


def _prioritize_columns(
    columns: list[dict], ndv: dict[str, int]
) -> list[dict]:
    """Sort columns for truncation: date > numeric > low-NDV categorical > rest."""

    def sort_key(col: dict) -> tuple[int, str]:
        col_type = col.get("type", "").upper()
        col_ndv = ndv.get(col["name"], -1)

        # Date/timestamp columns first
        if any(t in col_type for t in ("DATE", "TIMESTAMP", "TIME")):
            return (0, col["name"])
        # Numeric columns second
        if any(t in col_type for t in ("INT", "FLOAT", "DECIMAL", "NUMERIC", "DOUBLE", "REAL", "BIGINT")):
            return (1, col["name"])
        # Low-NDV categorical third
        if 0 < col_ndv < 500:
            return (2, col["name"])
        return (3, col["name"])

    return sorted(columns, key=sort_key)


def _generate_via_llm(
    physical_summary: dict[str, Any],
    config: IntentMapperConfig,
) -> SemanticSchema:
    """Call the LLM to generate a SemanticSchema from physical schema."""

    # Try instructor-based structured output first
    try:
        return _generate_with_instructor(physical_summary, config)
    except Exception as exc:
        logger.warning("Instructor-based generation failed: %s; falling back to JSON client", exc)

    # Fallback: raw JSON client
    return _generate_with_json_client(physical_summary, config)


def _generate_with_instructor(
    physical_summary: dict[str, Any],
    config: IntentMapperConfig,
) -> SemanticSchema:
    """Use instructor for structured LLM output."""
    import instructor

    user_prompt = (
        f"Physical database schema:\n{json.dumps(physical_summary, indent=2, default=str)}\n\n"
        "Generate a SemanticSchema JSON object."
    )

    if config.provider == "openai":
        from openai import OpenAI
        client = instructor.from_openai(OpenAI())
        return client.chat.completions.create(
            model=config.model or "gpt-4.1-mini",
            temperature=0,
            max_tokens=4096,
            response_model=SemanticSchema,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )

    if config.provider == "anthropic":
        from anthropic import Anthropic
        client = instructor.from_anthropic(Anthropic())
        return client.messages.create(
            model=config.model or "claude-sonnet-4-5",
            max_tokens=4096,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            response_model=SemanticSchema,
        )

    raise ValueError(f"Instructor not configured for provider: {config.provider}")


def _generate_with_json_client(
    physical_summary: dict[str, Any],
    config: IntentMapperConfig,
) -> SemanticSchema:
    """Fallback: use raw JSON completion client."""
    client = _build_completion_client(config.provider)
    user_prompt = (
        f"Physical database schema:\n{json.dumps(physical_summary, indent=2, default=str)}\n\n"
        "Generate a SemanticSchema JSON object."
    )

    raw = client.complete_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=config.model or "gpt-4.1-mini",
        timeout_ms=30000,
    )
    payload = json.loads(_strip_json_fences(raw))
    return SemanticSchema.model_validate(payload)


def _remove_broken_items(
    schema: SemanticSchema,
    validation: ValidationSummary,
) -> SemanticSchema:
    """Remove metrics/dimensions that failed validation."""
    broken_m = set(validation.broken_metrics)
    broken_d = set(validation.broken_dimensions)

    cleaned_metrics = [m for m in schema.metrics if m.name not in broken_m]
    cleaned_dims = [d for d in schema.dimensions if d.name not in broken_d]

    return schema.model_copy(update={
        "metrics": cleaned_metrics,
        "dimensions": cleaned_dims,
    })


def _persist_schema(
    schema: SemanticSchema,
    connection_id: str,
    version_id: str,
    schema_storage: "BaseSchemaStorage | None" = None,
) -> str:
    """Serialise the schema to YAML and persist it.

    When *schema_storage* is provided the YAML is written via the storage
    abstraction (local filesystem or S3).  The returned value is the path
    string that can later be passed back to ``schema_storage.load()``.
    """
    schema_dict = schema.model_dump(by_alias=True)
    yaml_content = yaml.dump(
        schema_dict, default_flow_style=False, sort_keys=False, allow_unicode=True
    )

    if schema_storage is not None:
        return schema_storage.save(connection_id, version_id, yaml_content)

    # Local fallback — write to data/schemas/{connection_id}/{version_id}.yaml
    out_dir = _SCHEMAS_DIR / connection_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{version_id}.yaml"
    out_path.write_text(yaml_content, encoding="utf-8")
    return str(out_path)
