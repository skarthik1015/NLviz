from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SemanticTable(BaseModel):
    name: str
    description: str | None = None
    # A link table pairs the keys of two others and holds nothing else (film_category: film_id,
    # category_id, last_update). Grouping through it by the other side repeats no row within a group.
    link: bool = False


class SemanticJoin(BaseModel):
    from_table: str = Field(alias="from")
    to_table: str = Field(alias="to")
    on: str
    join_type: Literal["LEFT", "INNER"] = Field(default="LEFT", alias="type")
    # "many_to_one": each from-row has at most one to-row (a foreign key from -> to). Known for
    # declared and "<table>_id" joins; None (hand-written schemas) means unknown, never checked.
    relationship: Literal["many_to_one"] | None = None

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
    # A dimension holding the unit of this metric's values (a currency column next to a cost): one
    # figure across several units is meaningless, so the agent asks (app/agent/ambiguity.py).
    unit_dimension: str | None = None
    # This metric is `excludes_zeros_of` with the rows where its column is 0 left out (zero_share of
    # them). Profiling proposes it when zeros look like "not recorded"; the user chooses (ambiguity).
    excludes_zeros_of: str | None = None
    zero_share: float | None = None

    @model_validator(mode="after")
    def check_aggregation_fields(self):
        if self.aggregation == "RATIO":
            if not self.numerator_sql or not self.denominator_sql:
                raise ValueError("RATIO metrics require numerator_sql and denominator_sql")
        elif not self.sql_expression:
            raise ValueError(f"{self.aggregation} metrics require sql_expression")
        return self


class SemanticDimension(BaseModel):
    name: str
    display_name: str
    sql_expression: str
    required_tables: list[str]
    cardinality: Literal["low", "medium", "high"] = "medium"
    # Value → other ways people say it ("SP": ["São Paulo"], "boleto": ["bank slip"]). Used by grounding.
    synonyms: dict[str, list[str]] | None = None
    # Another expression over the same tables whose value names the same thing (e.g. the Portuguese
    # category name). Only grounding uses it: it is indexed as an alias of this dimension's value.
    alias_expression: str | None = None
    # Values are lists ("French, Japanese, Desserts") separated by this character: a filter matches
    # one item, and a breakdown counts a row under each of its items (Phase 4).
    separator: Literal[",", ";", "|"] | None = None


class SemanticFilterColumn(SemanticDimension):
    """A column a question can put a condition on but never group by: "tracks longer than 5 minutes"
    (track.milliseconds > 300000), "rentals not returned" (rental.return_date IS NULL). Numbers and
    dates of the metrics' tables; the registry resolves them as filter targets only."""
    cardinality: Literal["low", "medium", "high"] = "high"
    kind: Literal["number", "date"]
    unit: str | None = None  # "milliseconds", "bytes": the LLM converts "5 minutes" into it
    description: str | None = None


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
    # Older published schemas have none: the feature appears when a schema is regenerated.
    filter_columns: list[SemanticFilterColumn] = Field(default_factory=list)
