from __future__ import annotations

from pathlib import Path

import yaml

from .models import (
    SemanticDimension,
    SemanticJoin,
    SemanticMetric,
    SemanticSchema,
    SemanticTimeDimension,
)


class SemanticRegistry:
    def __init__(self, schema: SemanticSchema):
        self.schema = schema
        self._metrics = {metric.name: metric for metric in schema.metrics}
        self._dimensions = {dimension.name: dimension for dimension in schema.dimensions}
        self._time_dimensions = {time_dimension.name: time_dimension for time_dimension in schema.time_dimensions}
        self._joins = schema.joins

    def get_metric(self, name: str) -> SemanticMetric:
        if name not in self._metrics:
            raise KeyError(f"Metric '{name}' is not defined in semantic schema")
        return self._metrics[name]

    def get_dimension(self, name: str) -> SemanticDimension:
        if name not in self._dimensions:
            raise KeyError(f"Dimension '{name}' is not defined in semantic schema")
        return self._dimensions[name]

    def get_time_dimension(self, name: str) -> SemanticTimeDimension:
        if name not in self._time_dimensions:
            raise KeyError(f"Time dimension '{name}' is not defined in semantic schema")
        return self._time_dimensions[name]

    def list_joins(self) -> list[SemanticJoin]:
        return self._joins

    def get_join_path(self, table_a: str, table_b: str) -> str | None:
        for join in self._joins:
            if join.from_table == table_a and join.to_table == table_b:
                return join.on
            if join.from_table == table_b and join.to_table == table_a:
                return join.on
        return None

    def to_prompt_context(self) -> str:
        lines: list[str] = []
        lines.append(f"Dataset: {self.schema.dataset}")
        lines.append(f"Description: {self.schema.description}")
        lines.append("")
        lines.append("Metrics:")
        for metric in self.schema.metrics:
            lines.append(f"- {metric.name} ({metric.display_name}): {metric.description}")
        lines.append("")
        lines.append("Dimensions:")
        for dimension in self.schema.dimensions:
            lines.append(
                f"- {dimension.name} ({dimension.display_name}): expression={dimension.sql_expression}"
            )
        lines.append("")
        lines.append("Time Dimensions:")
        for time_dimension in self.schema.time_dimensions:
            lines.append(
                f"- {time_dimension.name} ({time_dimension.display_name}), default_granularity={time_dimension.default_granularity}"
            )
        return "\n".join(lines)


def load_semantic_registry(schema_path: str | Path) -> SemanticRegistry:
    path = Path(schema_path)
    with path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp)
    schema = SemanticSchema.model_validate(raw)
    return SemanticRegistry(schema)


def load_semantic_registry_from_yaml(yaml_content: str) -> SemanticRegistry:
    """Build a SemanticRegistry from an already-loaded YAML string."""
    raw = yaml.safe_load(yaml_content)
    schema = SemanticSchema.model_validate(raw)
    return SemanticRegistry(schema)
