from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.llm import usage_meter
from app.models import ChatResponse, SemanticIntent
from app.models.responses import Interpretation

if TYPE_CHECKING:
    from app.agent.graph import QueryGraphRunner

# Conversation id given to a one-off question that paused for a clarification. Its thread is
# deleted once the question is answered.
ONE_OFF_PREFIX = "once-"


class QueryService:
    def __init__(self, query_graph: QueryGraphRunner):
        self.query_graph = query_graph

    def run_question(
        self,
        question: str,
        explicit_intent: SemanticIntent | None = None,
        debug: bool = False,
        *,
        conversation_id: str | None = None,
        scope: str = "local",
    ) -> ChatResponse:
        """`scope` (user and connection) namespaces the conversation, so a client can only reach
        its own threads whatever id it sends."""
        query_id = str(uuid4())
        conversation = conversation_id or f"{ONE_OFF_PREFIX}{uuid4().hex}"
        with usage_meter() as calls:
            state = self.query_graph.invoke(
                question=question,
                query_id=query_id,
                explicit_intent=explicit_intent,
                thread_id=_thread(scope, conversation),
                ephemeral=conversation.startswith(ONE_OFF_PREFIX),
            )
        return _response(state, conversation, debug, self._audit_run(), calls, self._registry())

    def resume(self, conversation_id: str, answer: str | None, debug: bool = False, *, scope: str = "local",
               remember: bool = False) -> ChatResponse:
        """Continue a turn paused on a clarification with the user's answer."""
        with usage_meter() as calls:
            state = self.query_graph.resume(
                thread_id=_thread(scope, conversation_id),
                answer={"id": answer, "remember": True} if remember else answer,
                ephemeral=conversation_id.startswith(ONE_OFF_PREFIX),
            )
        return _response(state, conversation_id, debug, self._audit_run(), calls, self._registry())

    def _audit_run(self) -> dict | None:
        return getattr(self.query_graph, "run_info", None)

    def _registry(self):
        dependencies = getattr(self.query_graph, "dependencies", None)
        return getattr(dependencies, "registry", None)

    def stream_question(
        self,
        question: str,
        explicit_intent: SemanticIntent | None = None,
        debug: bool = False,
        *,
        conversation_id: str | None = None,
        scope: str = "local",
    ) -> Iterator[dict[str, Any]]:
        """`run_question` as events: one "step" per graph node, then "result" (a ChatResponse)."""
        conversation = conversation_id or f"{ONE_OFF_PREFIX}{uuid4().hex}"
        events = self.query_graph.stream(
            thread_id=_thread(scope, conversation), ephemeral=conversation.startswith(ONE_OFF_PREFIX),
            question=question, query_id=str(uuid4()), explicit_intent=explicit_intent,
        )
        return _events(events, conversation, debug, self._audit_run(), self._registry())

    def stream_resume(
        self, conversation_id: str, answer: str | None, debug: bool = False, *, scope: str = "local",
        remember: bool = False,
    ) -> Iterator[dict[str, Any]]:
        events = self.query_graph.stream(
            thread_id=_thread(scope, conversation_id), ephemeral=conversation_id.startswith(ONE_OFF_PREFIX),
            answer={"id": answer, "remember": True} if remember else answer, resume=True,
        )
        return _events(events, conversation_id, debug, self._audit_run(), self._registry())


def _events(events, conversation: str, debug: bool, run: dict | None = None, registry=None) -> Iterator[dict[str, Any]]:
    for kind, payload in events:
        if kind == "step":
            node, update = payload
            step = {"event": "step", "node": node, "trace": [line for line in update.get("user_trace", []) if line]}
            timings = [t for t in update.get("timings_ms", []) if isinstance(t, dict)]
            if timings:
                step["ms"] = timings[-1]["ms"]
            evidence = [item for item in update.get("evidence", []) if isinstance(item, dict)]
            if evidence:
                step["evidence"] = evidence
            if debug:
                step["debug_trace"] = [line for line in update.get("debug_trace", []) if isinstance(line, str)]
            yield step
        else:
            yield {"event": "result", "response": _response(payload, conversation, debug, run, registry=registry)}


def _thread(scope: str, conversation: str) -> str:
    return f"{scope}/{conversation}"


def _interpretation(registry, intent, evidence):
    if registry is None:
        return None
    from app.services.interpretation import build_interpretation

    try:
        return build_interpretation(intent, registry, evidence)
    except Exception:  # the panel must never break an answer
        return None


def _sql_interpretation(state):
    """The SQL-primary node reads the panel from the SQL that ran (sql_verdict["interpretation"])."""
    found = (state.get("sql_verdict") or {}).get("interpretation")
    return Interpretation.model_validate(found) if found else None


def _response(state, conversation: str, debug: bool, run: dict | None = None,
              calls: list[dict] | None = None, registry=None) -> ChatResponse:
    intent = state.get("intent")
    if intent is None:
        raise RuntimeError("Query graph finished without an intent")
    clarification = state.get("clarification")
    unsupported = state.get("unsupported") if clarification is None else None
    if clarification is None and not unsupported and not state.get("sql"):
        raise RuntimeError("Query graph finished without SQL")
    one_off = conversation.startswith(ONE_OFF_PREFIX)

    return ChatResponse(
        query_id=state["query_id"],
        question=state["question"],
        intent=intent,
        intent_source=state.get("intent_source") or "heuristic",
        status="clarification" if clarification is not None else "unsupported" if unsupported else "answered",
        # A one-off question only needs its id while it waits for an answer.
        conversation_id=None if one_off and clarification is None else conversation,
        clarification=clarification,
        sql=state.get("sql") if clarification is None else None,
        rows=state.get("rows", []) if clarification is None else [],
        row_count=state.get("row_count", 0) if clarification is None else 0,
        trace=state.get("user_trace", []),
        validation_status=state.get("validation_status", ""),
        chart_spec=state.get("chart_spec") if clarification is None else None,
        explanation=state.get("explanation") if clarification is None else None,
        debug_trace=state.get("debug_trace") if debug else None,
        audit=_audit(state, run, calls) if debug else None,
        interpretation=None if unsupported else (
            Interpretation(path="generated_sql", notes=["SQL written by the model, not checked against the data "
                                                        "model's definitions."])
            if state.get("answer_path") == "generated_sql" else
            _sql_interpretation(state) if state.get("answer_path") == "sql" else
            _interpretation(registry, intent, state.get("evidence"))),
        evidence=state.get("evidence", []),
    )


def _audit(state, run: dict | None, calls: list[dict] | None) -> dict[str, Any]:
    audit: dict[str, Any] = {"run": run, "steps": [step for step in state.get("audit") or () if isinstance(step, dict)]}
    if calls is not None:
        audit["llm_calls"] = calls
        audit["tokens"] = {"prompt": sum(c.get("prompt_tokens") or 0 for c in calls),
                           "completion": sum(c.get("completion_tokens") or 0 for c in calls)}
    return audit
