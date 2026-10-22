"""Validate an auto-generated SemanticSchema against a real database."""
from __future__ import annotations

import logging

from app.connectors.base import DataConnector, SchemaContext
from app.models.connection import ValidationSummary
from app.models.semantic_intent import SemanticIntent
from app.semantic.loader import SemanticRegistry
from app.semantic.models import SemanticSchema
from app.semantic.sql_builder import build_sql_from_intent

logger = logging.getLogger(__name__)


def validate_schema(
    schema: SemanticSchema,
    connector: DataConnector,
    schema_ctx: SchemaContext,
) -> ValidationSummary:
    """Validate every metric and dimension by building + executing test queries.

    Returns a ValidationSummary with broken items identified.
    """
    registry = SemanticRegistry(schema)
    physical_tables = set(schema_ctx.tables.keys())

    broken_metrics: list[str] = []
    valid_metrics: list[str] = []

    # Validate metrics
    for metric in schema.metrics:
        if not _check_referential(metric.required_tables, physical_tables):
            broken_metrics.append(metric.name)
            logger.info("Metric '%s' failed referential check (missing tables)", metric.name)
            continue

        if _probe_metric(metric.name, registry, connector):
            valid_metrics.append(metric.name)
        else:
            broken_metrics.append(metric.name)
            logger.info("Metric '%s' failed execution probe", metric.name)

    broken_dimensions: list[str] = []
    valid_dimensions: list[str] = []
    for dim in schema.dimensions:
        if not _check_referential(dim.required_tables, physical_tables):
            broken_dimensions.append(dim.name)
            logger.info("Dimension '%s' failed referential check", dim.name)
            continue
        # A dimension may not join to every metric's tables: valid if it works with any metric.
        # Without a working metric nothing can be probed, and nothing unprobed counts as valid.
        if any(_probe_dimension(metric, dim.name, registry, connector) for metric in valid_metrics):
            valid_dimensions.append(dim.name)
        else:
            broken_dimensions.append(dim.name)
            logger.info("Dimension '%s' failed execution probe with every valid metric", dim.name)

    broken_time: list[str] = []
    valid_time: list[str] = []
    for time_dim in schema.time_dimensions:
        if time_dim.table in physical_tables and any(
            _probe_time_dimension(metric, time_dim.name, registry, connector) for metric in valid_metrics
        ):
            valid_time.append(time_dim.name)
        else:
            broken_time.append(time_dim.name)
            logger.info("Time dimension '%s' failed execution probe", time_dim.name)

    # Filter-only columns: a condition on each, with any working metric (they were never probed).
    broken_filters: list[str] = []
    for column in getattr(schema, "filter_columns", None) or []:
        if not (_check_referential(column.required_tables, physical_tables) and any(
                _probe_filter_column(metric, column.name, registry, connector) for metric in valid_metrics)):
            broken_filters.append(column.name)
            logger.info("Filter column '%s' failed execution probe", column.name)

    total_metrics = len(schema.metrics)
    confidence = len(valid_metrics) / total_metrics if total_metrics > 0 else 0.0

    return ValidationSummary(
        total_metrics=total_metrics,
        valid_metrics=len(valid_metrics),
        broken_metrics=broken_metrics,
        total_dimensions=len(schema.dimensions),
        valid_dimensions=len(valid_dimensions),
        broken_dimensions=broken_dimensions,
        total_time_dimensions=len(schema.time_dimensions),
        valid_time_dimensions=len(valid_time),
        broken_time_dimensions=broken_time,
        total_filter_columns=len(getattr(schema, "filter_columns", None) or []),
        broken_filter_columns=broken_filters,
        confidence_score=round(confidence, 3),
    )


def _check_referential(required_tables: list[str], physical_tables: set[str]) -> bool:
    """Check that all referenced tables exist in the physical schema."""
    for table_ref in required_tables:
        # required_tables entries can be "table_name" or "table_name alias"
        base_table = table_ref.split()[0]
        if base_table not in physical_tables:
            return False
    return True


def _probe_metric(
    metric_name: str,
    registry: SemanticRegistry,
    connector: DataConnector,
) -> bool:
    """Try to build and execute a minimal query for a single metric."""
    try:
        intent = SemanticIntent(
            metric=metric_name,
            dimensions=[],
            limit=1,
        )
        sql = build_sql_from_intent(intent, registry, table_ref=connector.table_ref_resolver())
        connector.execute_query(sql, limit=1)
        return True
    except Exception as exc:
        logger.info("Metric probe failed for %r: %s", metric_name, str(exc)[:300])
        return False


def _probe_dimension(
    metric_name: str,
    dimension_name: str,
    registry: SemanticRegistry,
    connector: DataConnector,
) -> bool:
    """Try to build and execute a query with a metric + dimension."""
    try:
        intent = SemanticIntent(
            metric=metric_name,
            dimensions=[dimension_name],
            limit=5,
        )
        sql = build_sql_from_intent(intent, registry, table_ref=connector.table_ref_resolver())
        connector.execute_query(sql, limit=5)
        return True
    except Exception as exc:
        logger.info("Dimension probe failed for %r: %s", dimension_name, str(exc)[:300])
        return False


def _probe_filter_column(
    metric_name: str,
    column_name: str,
    registry: SemanticRegistry,
    connector: DataConnector,
) -> bool:
    """Try a metric with a condition on the filter column (IS NOT NULL: any type takes it)."""
    from app.models.semantic_intent import FilterCondition

    try:
        intent = SemanticIntent(metric=metric_name, limit=1,
                                filters=[FilterCondition(dimension=column_name, operator="is_not_null")])
        sql = build_sql_from_intent(intent, registry, table_ref=connector.table_ref_resolver())
        connector.execute_query(sql, limit=1)
        return True
    except Exception as exc:
        logger.info("Filter column probe failed for %r: %s", column_name, str(exc)[:300])
        return False


def _probe_time_dimension(
    metric_name: str,
    time_dimension_name: str,
    registry: SemanticRegistry,
    connector: DataConnector,
) -> bool:
    """Try to build and execute a monthly trend of the metric over the time dimension."""
    try:
        intent = SemanticIntent(metric=metric_name, time_dimension=time_dimension_name, time_granularity="month",
                                order_by="time_asc", limit=5)
        sql = build_sql_from_intent(intent, registry, table_ref=connector.table_ref_resolver())
        connector.execute_query(sql, limit=5)
        return True
    except Exception as exc:
        logger.info("Time dimension probe failed for %r: %s", time_dimension_name, str(exc)[:300])
        return False
