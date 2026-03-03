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
    trace: TraceMessages


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
        "trace": [],
    }
