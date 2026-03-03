from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


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
    ]
    value: str | int | float | bool | list[str | int | float]


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
