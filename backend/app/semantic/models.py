from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SemanticTable(BaseModel):
    name: str
    description: str | None = None


class SemanticJoin(BaseModel):
    from_table: str = Field(alias="from")
    to_table: str = Field(alias="to")
    on: str
    join_type: Literal["LEFT", "INNER"] = Field(default="LEFT", alias="type")

    model_config = ConfigDict(populate_by_name=True)


class SemanticMetric(BaseModel):
    name: str
    display_name: str
    description: str
    aggregation: Literal["SUM", "AVG", "COUNT_DISTINCT", "COUNT", "RATIO"]
    sql_expression: str | None = None
    numerator_sql: str | None = None
    denominator_sql: str | None = None
    required_tables: list[str]
    base_filter: str | None = None


class SemanticDimension(BaseModel):
    name: str
    display_name: str
    sql_expression: str
    required_tables: list[str]
    cardinality: Literal["low", "medium", "high"] = "medium"


class SemanticTimeDimension(BaseModel):
    name: str
    display_name: str
    sql_expression: str
    default_granularity: Literal["day", "week", "month", "quarter", "year"] = "month"
    table: str


class SemanticSchema(BaseModel):
    version: str
    dataset: str
    description: str
    tables: list[SemanticTable]
    joins: list[SemanticJoin]
    metrics: list[SemanticMetric]
    dimensions: list[SemanticDimension]
    time_dimensions: list[SemanticTimeDimension]
