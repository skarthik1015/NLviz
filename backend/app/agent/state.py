from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

from app.models import SemanticIntent


TraceMessages = Annotated[list[str], add]


class AgentState(TypedDict, total=False):
    question: str
    query_id: str
    explicit_intent: SemanticIntent | None
    intent: SemanticIntent | None
    intent_source: str
    sql: str | None
    rows: list[dict[str, Any]]
    row_count: int
    # Dual-level traces: user_trace is friendly, debug_trace is technical
    user_trace: TraceMessages
    debug_trace: TraceMessages
    # Validator / self-correction
    validation_status: str  # "ok" | "empty" | "truncated" | "failed"
    validation_errors: list[str]
    retry_count: int
    correction_hint: str | None  # e.g. "remove_date_filters" or "reduce_limit:50"
    # Chart
    chart_spec: dict[str, Any] | None
    # Explanation
    explanation: str | None


def build_initial_state(
    *,
    question: str,
    query_id: str,
    explicit_intent: SemanticIntent | None = None,
) -> AgentState:
    return {
        "question": question,
        "query_id": query_id,
        "explicit_intent": explicit_intent,
        "intent": explicit_intent,
        "intent_source": "explicit" if explicit_intent is not None else "",
        "sql": None,
        "rows": [],
        "row_count": 0,
        "user_trace": [],
        "debug_trace": [],
        "validation_status": "",
        "validation_errors": [],
        "retry_count": 0,
        "correction_hint": None,
        "chart_spec": None,
        "explanation": None,
    }
