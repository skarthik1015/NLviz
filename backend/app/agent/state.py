from __future__ import annotations

from typing import Annotated, Any, TypedDict

from app.models import SemanticIntent

# State lives for a whole conversation (one checkpointer thread), but traces describe one turn:
# a list starting with TURN_START replaces the trace instead of extending it.
TURN_START = "\x00turn"


def _turn_trace(current: list | None, update: list | None) -> list:
    if update and update[0] == TURN_START:
        return list(update[1:])
    return [*(current or []), *(update or [])]


TraceMessages = Annotated[list, _turn_trace]

MAX_REPAIRS = 2


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
    # Evidence for the trace panel: values looked up, how they matched, clarifications answered.
    evidence: TraceMessages
    # Duration of each node this turn, {"node", "ms"}
    timings_ms: TraceMessages
    # What happened to the intent this turn, as data: each LLM proposal, rejection, value snap, rule
    # rewrite (before/after), clarification and engine error. For evals and debugging (debug=True).
    audit: TraceMessages
    # Validator / self-correction
    validation_status: str  # "ok" | "empty" | "truncated" | "failed"
    validation_errors: list[str]
    retry_count: int
    correction_hint: str | None  # e.g. "reduce_limit:50"
    # Repair: why the last attempt was rejected, fed back to the LLM (bounded by MAX_REPAIRS)
    repair_feedback: str | None
    repair_count: int
    mapping_error: str | None
    execution_error: str | None
    # What the registry is missing when the question cannot be answered (the turn ends without a query)
    unsupported: str | None
    # Why: "answerability" (the layer lacks it), "ambiguity" (double counting, mixed units),
    # "not_understood" (no valid reading). The SQL fallback may take the first and the last.
    refused_by: str | None
    # "generated_sql" when the SQL fallback answered, "sql" for the SQL-primary path (ANSWER_PATH=sql);
    # None for the semantic layer and compiler.
    answer_path: str | None
    # SQL-primary: the user's answers to this turn's choices ({reason, instruction}), and what the
    # checks found in the SQL that ran (notes, metrics it computes, definitions applied).
    sql_answered: list[dict[str, Any]]
    sql_verdict: dict[str, Any] | None
    # Definitions chosen in this conversation (measured columns -> constraint): kept across turns of
    # the thread, so follow-ups keep them; a new conversation starts without them. Not in
    # build_initial_state on purpose.
    policy_decisions: dict[str, dict[str, Any]]
    # Clarification waiting for the user's answer (the graph is paused on it)
    pending_clarification: dict[str, Any] | None
    # Conversation memory: the last answered turn of this thread, for follow-up questions
    previous_turn: dict[str, Any] | None
    # Value hints and examples looked up for this question, reused by repair rounds (same question)
    grounding_cache: dict[str, Any] | None
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
    """Input for a new turn. Keys not listed here (previous_turn) carry over from earlier turns."""
    return {
        "question": question,
        "query_id": query_id,
        "explicit_intent": explicit_intent,
        "intent": explicit_intent,
        "intent_source": "explicit" if explicit_intent is not None else "",
        "sql": None,
        "rows": [],
        "row_count": 0,
        "user_trace": [TURN_START],
        "debug_trace": [TURN_START],
        "evidence": [TURN_START],
        "timings_ms": [TURN_START],
        "audit": [TURN_START],
        "validation_status": "",
        "validation_errors": [],
        "retry_count": 0,
        "correction_hint": None,
        "repair_feedback": None,
        "repair_count": 0,
        "grounding_cache": None,
        "mapping_error": None,
        "execution_error": None,
        "pending_clarification": None,
        "unsupported": None,
        "refused_by": None,
        "answer_path": None,
        "sql_answered": [],
        "sql_verdict": None,
        "chart_spec": None,
        "explanation": None,
    }
