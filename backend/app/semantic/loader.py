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
        # Filter-only columns resolve like dimensions wherever a filter is read (compiler, explainer,
        # checks); the validator keeps them out of breakdowns.
        self._filter_columns = {c.name: c for c in schema.filter_columns if c.name not in self._dimensions}
        self._time_dimensions = {time_dimension.name: time_dimension for time_dimension in schema.time_dimensions}
        self._joins = schema.joins

    def get_metric(self, name: str) -> SemanticMetric:
        if name not in self._metrics:
            raise KeyError(f"Metric '{name}' is not defined in semantic schema")
        return self._metrics[name]

    def get_dimension(self, name: str) -> SemanticDimension:
        if name in self._dimensions:
            return self._dimensions[name]
        if name in self._filter_columns:
            return self._filter_columns[name]
        raise KeyError(f"Dimension '{name}' is not defined in semantic schema")

    def is_filter_only(self, name: str) -> bool:
        return name in self._filter_columns

    def get_time_dimension(self, name: str) -> SemanticTimeDimension:
        if name not in self._time_dimensions:
            raise KeyError(f"Time dimension '{name}' is not defined in semantic schema")
        return self._time_dimensions[name]

    def default_time_dimension(self, metric_name: str | None) -> str | None:
        """The date a period applies to when the intent names none: the metric's own table's first
        (employees hired in 1990: hire_date), else one of the metric's tables, else the schema's first.
        Taking the schema's first applied "in 1990" to the salary history through an extra join and
        halved the count (benchmark, 0d)."""
        times = self.schema.time_dimensions
        if not times:
            return None
        try:
            tables = [t.split()[0] for t in self.get_metric(metric_name).required_tables] if metric_name else []
        except KeyError:
            tables = []
        for table in tables:
            for time in times:
                if time.table == table:
                    return time.name
        return times[0].name

    def time_dimension_for(self, name: str) -> str | None:
        """The time dimension a name means: itself, or the time dimension over the same column as a
        dimension or filter column of that name (the LLM wrote time_dimension "employees_hire_date",
        the filter column, for the time dimension "hire_date")."""
        if name in self._time_dimensions:
            return name
        try:
            expression = self.get_dimension(name).sql_expression.replace(" ", "").lower()
        except KeyError:
            return None
        return next((t.name for t in self.schema.time_dimensions
                     if t.sql_expression.replace(" ", "").lower() == expression), None)

    def list_joins(self) -> list[SemanticJoin]:
        return self._joins


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
