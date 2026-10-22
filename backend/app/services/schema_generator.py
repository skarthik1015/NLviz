"""LLM-powered semantic schema generation from physical DB introspection."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from app.connectors.base import DataConnector, SchemaContext
from app.models.connection import GenerationMetadata, ValidationSummary
from app.semantic.models import SemanticSchema
from app.services.schema_completion import SchemaProposal, complete_schema
from app.llm import build_completion_client, is_budget_exceeded, strip_json_fences, structured_completion
from app.services.intent_mapper import IntentMapperConfig

if TYPE_CHECKING:
    from app.storage.schema_storage import BaseSchemaStorage

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "data" / "schemas"

_SYSTEM_PROMPT = """\
You are a data analyst building a semantic layer for a business database.
Given the physical schema below, propose the metrics, dimensions and time dimensions as JSON:
{"dataset": "short_name", "description": "one sentence",
 "metrics": [{"name": "total_revenue", "display_name": "Total Revenue", "description": "...",
              "aggregation": "SUM", "sql_expression": "payments.amount"}],
 "dimensions": [{"name": "payment_type", "display_name": "Payment Type",
                 "sql_expression": "payments.payment_type", "cardinality": "low"}],
 "time_dimensions": [{"name": "order_date", "display_name": "Order Date",
                      "sql_expression": "orders.created_at", "default_granularity": "month"}]}

Rules:
- 3-8 metrics: the most useful business measures. aggregation is one of SUM, AVG, COUNT_DISTINCT, COUNT, RATIO.
- Numeric measure columns: SUM and/or AVG. ID columns of the main entities: COUNT_DISTINCT.
- RATIO only for a clear good/bad status: give numerator_sql and denominator_sql instead of sql_expression,
  as row values that are summed, e.g. "CASE WHEN orders.status = 'canceled' THEN 1 ELSE 0 END" over "1"
  (the share of canceled orders). Use the status value exactly as it appears in the samples.
- sql_expression is the value BEFORE aggregation: write "payments.amount", never "SUM(payments.amount)".
- Name a metric for what its column holds. Do not give it a meaning the data does not have: a sum of
  "average_cost_for_two" is not revenue, a count of reviews is not a count of customers.
- Qualify every column with its table name exactly as listed (table.column). Do not invent aliases.
- 3-10 dimensions: categorical columns with fewer than 500 distinct values. cardinality: low (< 20), medium (< 500), high (>= 500).
- Time dimensions: only DATE or TIMESTAMP columns (never an integer year column).
- SQL must run on Apache DataFusion: no DATEDIFF/date_diff (days between dates: CAST(b AS DATE) - CAST(a AS DATE)),
  no year()/strftime() (use date_part or DATE_TRUNC), cast with CAST(x AS DOUBLE).
- base_filter: optional, only for clear data-quality reasons.
- Names in snake_case; display_name in Title Case.

Output ONLY the JSON object. No markdown fences."""


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
    config = (config or IntentMapperConfig.from_env()).for_task("schema")

    start_ms = time.perf_counter()

    # Step 1: Introspect physical schema
    schema_ctx = connector.get_schema()
    physical_summary = _build_physical_summary(schema_ctx)
    table_count = len(schema_ctx.tables)
    column_count = sum(len(cols) for cols in schema_ctx.tables.values())

    # Step 2: the LLM proposes metrics and dimensions; tables, joins and required tables are
    # derived from introspection, and column references repaired (schema_completion).
    proposal = _generate_via_llm(physical_summary, config)
    semantic_schema, _notes = complete_schema(proposal, schema_ctx)
    semantic_schema = exclude_placeholder_zeros(semantic_schema, connector)
    semantic_schema = detect_list_dimensions(semantic_schema, schema_ctx, connector)
    semantic_schema = detect_unit_dimensions(semantic_schema, connector)
    if not semantic_schema.metrics:
        raise ValueError("The generated schema has no usable metrics; try again or edit the schema by hand")

    generation_time_ms = round((time.perf_counter() - start_ms) * 1000, 2)

    # Step 3: Validate via execution probes
    from app.services.schema_validator import validate_schema

    validation = validate_schema(semantic_schema, connector, schema_ctx)

    # Step 4: Clean broken items & persist
    cleaned = _remove_broken_items(semantic_schema, validation)

    if not cleaned.metrics:
        raise ValueError("None of the generated metrics ran on this data; try again or edit the schema by hand")
    clean_validation = validation.model_copy(update={
        "published_metrics": len(cleaned.metrics),
        "published_dimensions": len(cleaned.dimensions),
        "published_time_dimensions": len(cleaned.time_dimensions),
        "published_filter_columns": len(cleaned.filter_columns),
    })

    yaml_path = _persist_schema(cleaned, connection_id, version_id, schema_storage)

    metadata = GenerationMetadata(
        llm_provider=config.provider or "unknown",
        llm_model=config.model or "unknown",
        generation_time_ms=generation_time_ms,
        table_count=table_count,
        column_count=column_count,
    )

    return cleaned, yaml_path, clean_validation, metadata


_BARE_COLUMN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$")
# 0 stands for "no value" when it is common and far below every real value: a rating of 0 on 22%
# of the rows of a restaurant file whose real ratings run 1.8-4.9 ("Not rated").
_PLACEHOLDER_MIN_SHARE = 0.05
_PLACEHOLDER_MIN_GAP = 0.2  # the smallest non-zero value is at least this fraction of the largest
# Only on a scale that has no zero: a 0 rating means "not rated", a 0 discount or 0 quantity is a real
# value and averaging it out would change the answer (review B10: 20 zeros and 80 tens average 8, not 10).
_SCALE_COLUMN = re.compile(r"(rating|score|stars|grade)", re.IGNORECASE)


def exclude_placeholder_zeros(schema: SemanticSchema, connector: DataConnector) -> SemanticSchema:
    """A second average that skips zeros, next to the original, where zeros look like "not
    recorded" (a rating of 0 on a fifth of the restaurant file, whose real ratings run 1.8-4.9).
    The original is never redefined: a column name and a distribution cannot prove that 0 means
    missing (second review: an exam score with real zeros changed from 8 to 10). The agent asks which
    one a question means (app/agent/ambiguity.py, zero_placeholder)."""
    from app.semantic.sql_builder import _format_table_with_alias

    resolver = connector.table_ref_resolver()
    metrics = []
    for metric in schema.metrics:
        match = _BARE_COLUMN.match(metric.sql_expression or "") if metric.aggregation == "AVG" else None
        if match is None:
            metrics.append(metric)
            continue
        table, column = match.groups()
        if not _SCALE_COLUMN.search(column):
            metrics.append(metric)
            continue
        expression = f"{table}.{column}"
        sql = (f"SELECT COUNT(*) AS n, SUM(CASE WHEN {expression} = 0 THEN 1 ELSE 0 END) AS zeros, "
               f"MIN(CASE WHEN {expression} <> 0 THEN {expression} END) AS low, MAX({expression}) AS high "
               f"FROM {_format_table_with_alias(table, table, resolver)}")
        try:
            row = connector.execute_query(sql, limit=1).iloc[0]
            n, zeros, low, high = (float(row[k]) if row[k] is not None else 0.0 for k in ("n", "zeros", "low", "high"))
        except Exception as exc:  # profiling must never fail generation
            logger.info("Zero profile failed for %s: %s", metric.name, str(exc)[:200])
            metrics.append(metric)
            continue
        metrics.append(metric)
        if n > 0 and zeros / n >= _PLACEHOLDER_MIN_SHARE and low > 0 and high > 0 and low >= _PLACEHOLDER_MIN_GAP * high:
            name = f"{metric.name}_excluding_zero"
            if any(m.name == name for m in schema.metrics):
                continue
            logger.info("Schema completion: %s may use 0 for 'not recorded' (%d of %d rows; real values %s-%s)",
                        metric.name, zeros, n, low, high)
            metrics.append(metric.model_copy(update={
                "name": name,
                "display_name": f"{metric.display_name} (excluding zeros)",
                "sql_expression": f"NULLIF({expression}, 0)",
                "description": f"{metric.description}, leaving out the {zeros / n:.0%} of rows where it is 0",
                "excludes_zeros_of": metric.name,
                "zero_share": round(zeros / n, 3),
            }))
    return schema.model_copy(update={"metrics": metrics})


_LIST_SEPARATORS = (",", ";", "|")
_LIST_MIN_SHARE = 0.2        # rows whose value has the separator
_LIST_MIN_DISTINCT = 20      # fewer distinct values: a plain category, even if some contain a comma ...
_LIST_SMALL_MIN_DISTINCT = 6  # ... unless most rows hold the separator (Pagila's 15 special-feature lists)
_LIST_SMALL_MIN_SHARE = 0.5
_LIST_MAX_ITEMS = 500
_LIST_MAX_ITEM_RATIO = 0.5   # items per distinct value: free text and addresses split into ever new parts


def detect_list_dimensions(schema: SemanticSchema, ctx: SchemaContext, connector: DataConnector) -> SemanticSchema:
    """Text columns holding lists ("French, Japanese, Desserts"): their dimension gets a separator,
    so a filter matches one item and a breakdown counts each item; a list column the LLM left out
    becomes a dimension. A restaurant file's cuisines column has 1,825 distinct lists of 145
    cuisines: as plain text, "Chinese restaurants" (cuisines = 'Chinese') found 354 of 2,733.
    Two profiling queries per text column with at least 20 distinct values; any failure leaves
    the column as it is."""
    from app.semantic.models import SemanticDimension
    from app.semantic.sql_builder import _format_table_with_alias, items_expression

    resolver = connector.table_ref_resolver()
    by_expression = {d.sql_expression.strip(): i for i, d in enumerate(schema.dimensions)}
    names = {d.name for d in schema.dimensions} | {m.name for m in schema.metrics}
    dimensions = list(schema.dimensions)
    for table, columns in ctx.tables.items():
        source = _format_table_with_alias(table, table, resolver)
        for col in columns:
            name = col["name"]
            distinct = ctx.distinct_counts.get(table, {}).get(name, -1)
            if any(t in col.get("type", "").upper() for t in _NUMERIC_TYPES + ("DATE", "TIME", "BOOL")) \
                    or distinct < _LIST_SMALL_MIN_DISTINCT:
                continue
            expression = f"{table}.{name}"
            try:
                shares = connector.execute_query(
                    "SELECT COUNT(" + expression + ") AS n, "
                    + ", ".join(f"SUM(CASE WHEN {expression} LIKE '%{s}%' THEN 1 ELSE 0 END) AS s{i}"
                                for i, s in enumerate(_LIST_SEPARATORS))
                    + f" FROM {source}", limit=1).iloc[0]
                n = float(shares["n"] or 0)
                share, separator = max((float(shares[f"s{i}"] or 0) / n if n else 0.0, s)
                                       for i, s in enumerate(_LIST_SEPARATORS))
                if share < _LIST_MIN_SHARE or (distinct < _LIST_MIN_DISTINCT and share < _LIST_SMALL_MIN_SHARE):
                    continue
                items = int(connector.execute_query(
                    f"SELECT COUNT(DISTINCT item) AS items FROM (SELECT unnest({items_expression(expression, separator)}) "
                    f"AS item FROM {source}) AS listed", limit=1).iloc[0]["items"])
            except Exception as exc:  # profiling must never fail generation
                logger.info("List profile failed for %s: %s", expression, str(exc)[:200])
                continue
            if not 2 <= items <= _LIST_MAX_ITEMS or items > _LIST_MAX_ITEM_RATIO * distinct:
                continue
            cardinality = "low" if items < 20 else "medium"
            logger.info("Schema completion: %s holds lists (%d items in %d distinct values, separator %r)",
                        expression, items, distinct, separator)
            if expression in by_expression:
                i = by_expression[expression]
                dimensions[i] = dimensions[i].model_copy(update={"separator": separator, "cardinality": cardinality})
            else:
                dimension_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
                if dimension_name in names:
                    dimension_name = f"{table}_{dimension_name}"
                names.add(dimension_name)
                dimensions.append(SemanticDimension(
                    name=dimension_name, display_name=name.replace("_", " ").title(), sql_expression=expression,
                    required_tables=[table], cardinality=cardinality, separator=separator))
    return schema.model_copy(update={"dimensions": dimensions})


_NUMERIC_TYPES = ("INT", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL")

_CURRENCY_VALUE = re.compile(
    r"(rupee|rupiah|dollar|real|euro|pound|lira|rand|rial|riyal|dirham|diram|pula|peso|yen|yuan|renminbi|won|"
    r"franc|krona|krone|ringgit|baht|dinar|shekel|zloty|forint|naira|shilling|[$€£¥₹]|"
    r"\b(usd|eur|gbp|inr|jpy|cny|aud|cad|brl|idr|zar|try|aed|qar|lkr|nzd|php|sgd|chf|sek|nok|mxn|krw)\b)",
    re.IGNORECASE)
# The name ends in the unit word: "currency", "price_currency", "uom"; not "unit_price" (Chinook's
# 0.99/1.99 prices were taken for currencies and every line amount asked "which unit?").
_UNIT_COLUMN = re.compile(r"(^|_)(currency|currencies|ccy|unit|units|uom)$")
_MONEY_COLUMN = re.compile(r"(cost|price|amount|revenue|sales|spend|fee|salary|income|budget|value|payment|fare|"
                           r"charge|wage|profit|tax|total)")
# "price_range" is a level (1-4), not an amount of money.
_NOT_MONEY = re.compile(r"(range|level|tier|grade|band|class|rating|score|rank|count|qty|quantity|pct|percent|ratio)")
_UNIT_MIN_SHARE = 0.6
_PACK_SIZE = re.compile(r"(per_unit|quantity|qty|pack|size)")


def detect_unit_dimensions(schema: SemanticSchema, connector: DataConnector) -> SemanticSchema:
    """Link money metrics to a currency column on the same table (`unit_dimension`), so the agent
    asks instead of adding or averaging across currencies. A restaurant file's "average cost for
    two" mixed 12 currencies into one figure (an Indonesian Rupiah row weighs 10,000 Indian Rupee
    rows). A currency column: named currency/unit, or most of its values look like currencies.
    One DISTINCT query per low/medium-cardinality dimension that is a plain column."""
    from app.semantic.sql_builder import _format_table_with_alias

    resolver = connector.table_ref_resolver()
    units: dict[str, str] = {}  # table -> unit dimension name
    for dimension in schema.dimensions:
        match = _BARE_COLUMN.match(dimension.sql_expression)
        if match is None or dimension.cardinality == "high" or dimension.separator:
            continue
        table, column = match.groups()
        if table in units:
            continue
        try:
            values = [str(v) for v in connector.execute_query(
                f"SELECT DISTINCT {table}.{column} AS v FROM {_format_table_with_alias(table, table, resolver)}",
                limit=100)["v"].dropna()]
        except Exception as exc:  # profiling must never fail generation
            logger.info("Unit profile failed for %s: %s", dimension.name, str(exc)[:200])
            continue
        currency_like = sum(bool(_CURRENCY_VALUE.search(v)) for v in values)
        if all(re.fullmatch(r"-?[\d.,]+", v) for v in values):
            continue  # numbers are amounts or levels, never units
        # A pack size is no unit of the price: Northwind's quantity_per_unit ("10 boxes x 20 bags") had
        # "average list price by category" asking which unit to use.
        named_unit = _UNIT_COLUMN.search(column.lower()) and not _PACK_SIZE.search(column.lower())
        if len(values) >= 2 and (named_unit or currency_like >= _UNIT_MIN_SHARE * len(values)):
            units[table] = dimension.name
            logger.info("Schema completion: %s holds units (%d values, %d currency-like)", dimension.name,
                        len(values), currency_like)
    if not units:
        return schema
    metrics = []
    for metric in schema.metrics:
        columns = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                             " ".join(filter(None, (metric.sql_expression, metric.numerator_sql))))
        tables = {table for table, column in columns
                  if _MONEY_COLUMN.search(column.lower()) and not _NOT_MONEY.search(column.lower())}
        unit = next((units[t] for t in tables if t in units), None)
        if unit and metric.aggregation in ("SUM", "AVG"):
            metric = metric.model_copy(update={"unit_dimension": unit})
            logger.info("Schema completion: %s is in the units of %s", metric.name, unit)
        metrics.append(metric)
    return schema.model_copy(update={"metrics": metrics})


# Sample values help only where a column is a candidate dimension; for IDs and free text they cost
# tokens and teach nothing.
_SAMPLE_MAX_DISTINCT = 500
_SAMPLE_MAX_CHARS = 30


def _build_physical_summary(ctx: SchemaContext) -> str:
    """A compact, token-budgeted text description of the physical schema for the LLM.

    One line per column ("name TYPE, N distinct, e.g. a | b | c") instead of indented JSON: the
    8-table demo went from ~4,900 to ~1,500 prompt tokens, so it fits a local model's context with
    room for the answer."""
    total_columns = sum(len(columns) for columns in ctx.tables.values())
    truncate = total_columns > 80  # token budget: then the top 5 columns per table
    lines: list[str] = []
    for table_name, columns in ctx.tables.items():
        ndv = ctx.distinct_counts.get(table_name, {})
        col_list = _prioritize_columns(columns, ndv)[:5] if truncate else columns
        lines.append(f"TABLE {table_name} ({ctx.row_counts.get(table_name, 0)} rows)")
        for col in col_list:
            parts = [f"  {col['name']} {col['type']}"]
            distinct = ndv.get(col["name"])
            if distinct is not None and distinct >= 0:
                parts.append(f"{distinct} distinct")
                samples = [str(v)[:_SAMPLE_MAX_CHARS] for v in (col.get("sample_values") or [])[:3]]
                if samples and distinct < _SAMPLE_MAX_DISTINCT and not _is_temporal(col):
                    parts.append("e.g. " + " | ".join(samples))
            lines.append(", ".join(parts))
    joins = list(ctx.join_paths) + list(ctx.inferred_joins)
    if joins:
        lines.append("JOINS")
        lines.extend(f"  {join['on']}" for join in joins)
    return "\n".join(lines)


def _is_temporal(col: dict) -> bool:
    return any(t in col.get("type", "").upper() for t in ("DATE", "TIMESTAMP", "TIME"))


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


def _user_prompt(physical_summary: str) -> str:
    return f"Physical database schema:\n{physical_summary}\n\nGenerate a SemanticSchema JSON object."


def _generate_via_llm(
    physical_summary: str,
    config: IntentMapperConfig,
) -> SchemaProposal:
    """Call the LLM for a schema proposal."""

    # Try instructor-based structured output first
    try:
        return _generate_with_instructor(physical_summary, config)
    except Exception as exc:
        if is_budget_exceeded(exc):
            raise  # the JSON fallback is another hosted call
        logger.warning("Instructor-based generation failed: %s; falling back to JSON client", exc)

    # Fallback: raw JSON client
    return _generate_with_json_client(physical_summary, config)


_SCHEMA_MAX_TOKENS = 4096  # a proposal is ~2-3k tokens; the 800 used for intents would truncate it


def _generate_with_instructor(
    physical_summary: str,
    config: IntentMapperConfig,
) -> SchemaProposal:
    """Use instructor for structured LLM output."""
    if not config.model:
        raise ValueError("LLM_MODEL (or LLM_MODEL_SCHEMA) must be set for schema generation")
    return structured_completion(
        provider=config.provider, model=config.model, system_prompt=_SYSTEM_PROMPT,
        user_prompt=_user_prompt(physical_summary), response_model=SchemaProposal, timeout_ms=120000,
        max_tokens=_SCHEMA_MAX_TOKENS,
        # One attempt: a retry re-generates the whole answer (minutes on a local model), and the
        # JSON fallback below is cheaper.
        max_retries=1,
    )


def _generate_with_json_client(
    physical_summary: str,
    config: IntentMapperConfig,
) -> SchemaProposal:
    """Fallback: use raw JSON completion client."""
    if not config.model:
        raise ValueError("LLM_MODEL (or LLM_MODEL_SCHEMA) must be set for schema generation")
    raw = build_completion_client(config.provider).complete_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_user_prompt(physical_summary),
        model=config.model,
        timeout_ms=120000,
        max_tokens=_SCHEMA_MAX_TOKENS,
    )
    payload = json.loads(strip_json_fences(raw))
    return SchemaProposal.model_validate(payload)


def _remove_broken_items(
    schema: SemanticSchema,
    validation: ValidationSummary,
) -> SemanticSchema:
    """Remove metrics/dimensions that failed validation."""
    broken_m = set(validation.broken_metrics)
    broken_d = set(validation.broken_dimensions)
    broken_t = set(validation.broken_time_dimensions)
    broken_f = set(validation.broken_filter_columns)
    dimensions = [d for d in schema.dimensions if d.name not in broken_d]
    kept = {d.name for d in dimensions}
    # A metric's unit column that was removed is no unit column any more.
    metrics = [m if not m.unit_dimension or m.unit_dimension in kept else m.model_copy(update={"unit_dimension": None})
               for m in schema.metrics if m.name not in broken_m]

    return schema.model_copy(update={
        "metrics": metrics,
        "dimensions": dimensions,
        "time_dimensions": [t for t in schema.time_dimensions if t.name not in broken_t],
        "filter_columns": [f for f in schema.filter_columns if f.name not in broken_f],
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
