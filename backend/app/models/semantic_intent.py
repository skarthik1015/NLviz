from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema


class FilterCondition(BaseModel):
    dimension: str
    operator: Literal[
        "eq",
        "ne",
        "in",
        "not_in",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "between",
        "is_null",
        "is_not_null",
    ]
    # No value for is_null / is_not_null ("rentals not returned yet": return_date is_null).
    value: str | int | float | bool | list[str | int | float] | None = None


class MetricFilter(BaseModel):
    """A threshold on an aggregate, per group: "albums with more than 5 tracks" is tracks > 5 by album.
    Compiled to HAVING."""
    metric: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: float


class SemanticIntent(BaseModel):
    metric: str = Field(..., description="Canonical metric name from semantic registry")
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterCondition] = Field(default_factory=list)
    time_dimension: str | None = None
    time_granularity: Literal["day", "week", "month", "quarter", "year"] | None = None
    start_date: date | None = None
    end_date: date | None = None
    order_by: Literal["metric_desc", "metric_asc", "time_asc", "time_desc"] = "metric_desc"
    limit: int = Field(default=100, ge=1, le=5000)
    # Set by the answerability check (not by the intent LLM, and kept out of its schema) when the
    # question needs something the registry does not have; the agent then says so instead of
    # answering a different question with the closest metric.
    unsupported: SkipJsonSchema[str | None] = None
    # Thresholds on metrics (HAVING). Not in the LLM's schema: it writes them as filters on a metric's
    # name, and validation moves them here, so every other filter is on a real dimension.
    metric_filters: SkipJsonSchema[list[MetricFilter]] = Field(default_factory=list)
