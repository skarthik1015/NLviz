from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .semantic_intent import SemanticIntent


# Client-chosen conversation ids; the server scopes them by user and connection.
_CONVERSATION_ID = r"^[A-Za-z0-9_-]{1,64}$"


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    intent: SemanticIntent | None = None
    debug: bool = False
    # Questions with the same id form one conversation: follow-ups see the previous turn.
    conversation_id: str | None = Field(default=None, pattern=_CONVERSATION_ID)


class ChatResumeRequest(BaseModel):
    """The user's answer to a clarification. `answer` is one of the offered options, the
    clarification's `any_value`, or free text."""
    conversation_id: str = Field(..., pattern=_CONVERSATION_ID)
    answer: str | None = Field(default=None, max_length=200)
    debug: bool = False
    # For a definition question: keep the answer for every question on this connection (owner-scoped).
    remember: bool = False


class ClarificationChoice(BaseModel):
    id: str
    label: str


class Clarification(BaseModel):
    # "value": which value of a dimension (answer with one of `options`, or `any_value`).
    # "choice": an ambiguity the data reveals (answer with a choice `id`).
    kind: Literal["value", "choice"]
    # "no_match": `raw` (the LLM's value) matches nothing confidently; "assumed": the LLM chose
    # `raw`, a real value the question does not name, among several close ones; "mixed_units": the
    # answer would add or average values in different units (currencies); "fan_out": the metric
    # would be counted once per row of a one-to-many join (choose a measure recorded per row).
    # "definition": which business definition to use (an unconfirmed condition of the data model).
    reason: Literal["no_match", "assumed", "mixed_units", "fan_out", "zero_placeholder", "definition"] = "no_match"
    prompt: str
    dimension: str | None = None  # none for a choice about the metric
    raw: str = ""
    options: list[str] = Field(default_factory=list)
    any_value: str = ""
    choices: list[ClarificationChoice] = Field(default_factory=list)
    # The answer may be kept for every question on this connection (ChatResumeRequest.remember).
    rememberable: bool = False


class InterpretedMetric(BaseModel):
    name: str
    label: str
    aggregation: str
    definition: str  # what is computed, e.g. "SUM(order_payments.payment_value) where ..."
    description: str = ""


class InterpretedField(BaseModel):
    name: str
    label: str


class InterpretedFilter(BaseModel):
    dimension: str
    label: str
    operator: str
    value: Any = None


class InterpretedPeriod(BaseModel):
    column: str  # the time dimension the period applies to
    label: str
    start: date | None = None
    end: date | None = None  # inclusive


class InterpretedPolicy(BaseModel):
    """A business decision behind the metric ("revenue excludes canceled orders"), step 1b."""
    name: str
    statement: str
    # inferred: the data model's guess; confirmed: the owner's, for this connection; conversation: the
    # user's choice in this conversation; question: what the question asked for.
    source: Literal["inferred", "confirmed", "conversation", "question", "unknown"] = "inferred"


class Interpretation(BaseModel):
    """How the answer was understood, for the interpretation panel (step 1a). `path` says which
    way it was produced: the semantic layer and compiler ("semantic"), SQL the model wrote and the
    checks held to the layer ("sql", read from that SQL), or the unchecked fallback ("generated_sql")."""
    path: Literal["semantic", "sql", "generated_sql"] = "semantic"
    metric: InterpretedMetric | None = None
    # "sql": every aggregate matched to a metric of the layer (a query can compute several).
    measures: list[InterpretedMetric] = Field(default_factory=list)
    sql: str | None = None  # "sql": the query the interpretation was read from
    filters: list[InterpretedFilter] = Field(default_factory=list)
    thresholds: list[str] = Field(default_factory=list)  # per group, e.g. "track_count gt 5"
    grouping: list[InterpretedField] = Field(default_factory=list)
    time_grain: InterpretedField | None = None  # e.g. {"name": "order_date", "label": "Order Date by month"}
    period: InterpretedPeriod | None = None
    order_by: str = "metric_desc"
    limit: int = 100
    policies: list[InterpretedPolicy] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # from the checks: double counting, zeros, units


class ChatResponse(BaseModel):
    query_id: str
    question: str
    intent: SemanticIntent
    intent_source: Literal["heuristic", "llm", "llm_fallback", "explicit"]
    # "clarification": the turn is paused on `clarification`; answer it with POST /chat/resume.
    # "unsupported": the data cannot answer the question; `explanation` says what is missing.
    status: Literal["answered", "clarification", "unsupported"] = "answered"
    conversation_id: str | None = None
    clarification: Clarification | None = None
    sql: str | None
    rows: list[dict[str, Any]]
    row_count: int
    trace: list[str]  # user-friendly trace, always present
    validation_status: str = ""
    chart_spec: dict[str, Any] | None = None
    explanation: str | None = None
    debug_trace: list[str] | None = None  # only populated when request.debug=True
    # Only with debug=True: {"run": model, phrase rules on/off, semantic layer version; "steps": each
    # proposal, rejection, snap, rule rewrite (before/after), clarification and engine error this
    # turn; "llm_calls": tokens per LLM call (run_question and resume only)}.
    audit: dict[str, Any] | None = None
    # How values in the question were matched to the data (value hints, snaps, clarifications).
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # How the answer was understood (step 1a); none for a refusal.
    interpretation: Interpretation | None = None


class SchemaResponse(BaseModel):
    connector_type: str
    tables: dict[str, list[dict[str, Any]]]
    row_counts: dict[str, int]
    join_paths: list[dict[str, str]]
    metrics: list[dict[str, str]]
    dimensions: list[dict[str, str]]
    time_dimensions: list[dict[str, str]]
    # Example questions the data model answers as asked (app.semantic.suggestions).
    suggested_questions: list[str] = []
    # Value grounding for this connection: "ready", "building" (answers come without value
    # matching until it is ready), "none" or "off". Evals wait on this instead of sleeping.
    grounding: str = "off"


class FeedbackRequest(BaseModel):
    query_id: str
    rating: Literal["positive", "negative"]
    comment: str | None = None
    idempotency_key: str | None = None


class FeedbackRecord(BaseModel):
    feedback_id: str
    query_id: str
    rating: Literal["positive", "negative"]
    comment: str | None = None
    created_at: datetime
    idempotency_key: str | None = None
    owner_id: str | None = None


class FeedbackResponse(BaseModel):
    status: Literal["created", "updated"]
    feedback: FeedbackRecord
