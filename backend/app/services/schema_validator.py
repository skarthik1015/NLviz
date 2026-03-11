"""Validate an auto-generated SemanticSchema against a real database."""
from __future__ import annotations

import logging
from typing import Any

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

    # Need at least one valid metric to test dimensions
    first_valid_metric = valid_metrics[0] if valid_metrics else None

    broken_dimensions: list[str] = []
    valid_dimensions: list[str] = []

    for dim in schema.dimensions:
        if not _check_referential(dim.required_tables, physical_tables):
            broken_dimensions.append(dim.name)
            logger.info("Dimension '%s' failed referential check", dim.name)
            continue

        if first_valid_metric and _probe_dimension(first_valid_metric, dim.name, registry, connector):
            valid_dimensions.append(dim.name)
        elif first_valid_metric:
            broken_dimensions.append(dim.name)
            logger.info("Dimension '%s' failed execution probe", dim.name)
        else:
            # No valid metric to test with — assume dimension is OK structurally
            valid_dimensions.append(dim.name)

    total_metrics = len(schema.metrics)
    confidence = len(valid_metrics) / total_metrics if total_metrics > 0 else 0.0

    return ValidationSummary(
        total_metrics=total_metrics,
        valid_metrics=len(valid_metrics),
        broken_metrics=broken_metrics,
        total_dimensions=len(schema.dimensions),
        valid_dimensions=len(valid_dimensions),
        broken_dimensions=broken_dimensions,
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
        sql = build_sql_from_intent(intent, registry)
        connector.execute_query(sql, limit=1)
        return True
    except Exception as exc:
        logger.debug("Metric probe failed for '%s': %s", metric_name, exc)
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
        sql = build_sql_from_intent(intent, registry)
        connector.execute_query(sql, limit=5)
        return True
    except Exception as exc:
        logger.debug("Dimension probe failed for '%s': %s", dimension_name, exc)
        return False
