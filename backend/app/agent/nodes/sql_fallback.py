"""The guarded SQL fallback (SQL_FALLBACK=on): a question the semantic layer cannot answer, or that
has no valid reading, is answered with SQL the model writes from the same context the intent mapper
gets, through the pushdown guard and the safety gate (app.services.generated_sql). The answer is
labelled generated_sql. Refusals for double counting or mixed units never come here: those are
answers about the data, not gaps in the layer.
"""
from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.services.generated_sql import generate_and_run
from app.services.intent_mapper import IntentMappingError
from app.services.text_to_sql import SYSTEM_PROMPT, build_context, to_physical

FALLBACK_REASONS = ("answerability", "not_understood")


def build_sql_fallback_node(dependencies) -> Callable[[AgentState], AgentState]:
    registry, schema, mapper = dependencies.registry, dependencies.schema_context, dependencies.intent_mapper

    def node(state: AgentState) -> AgentState:
        question = state.get("question", "")
        hints = (state.get("grounding_cache") or {}).get("hints") or mapper._value_hints(question)
        examples = (state.get("grounding_cache") or {}).get("examples")
        previous = state.get("previous_turn") or {}
        previous_sql = (previous["question"], previous["sql"]) if previous.get("sql") else None
        result = generate_and_run(question, build_context(registry, schema, hints, examples), previous_sql,
                                  dependencies.connector, lambda sql: to_physical(sql, registry, dependencies.table_ref),
                                  dependencies.intent_config, system_prompt=SYSTEM_PROMPT, guard=True)
        audit = [{"stage": "sql_fallback", "reason": state.get("refused_by")}, *result["steps"]]
        if result["status"] != "answered":
            if state.get("refused_by") == "not_understood":
                raise IntentMappingError("No valid reading, and the SQL fallback found none: "
                                         f"{result['error'] or result['missing']}")
            return {"audit": audit, "debug_trace": [f"SQL fallback: {result['status']} ({result['error'] or result['missing']})"]}
        rows = result["rows"]
        return {
            "unsupported": None,
            "answer_path": "generated_sql",
            "sql": result["sql"],
            "rows": rows,
            "row_count": len(rows),
            "validation_status": "ok",
            "chart_spec": None,
            "explanation": (f"Answered with SQL written by the model, not checked against the data model's "
                            f"definitions: {len(rows)} row(s). Check the query before relying on it."),
            # A follow-up builds on this SQL; there is no intent to keep.
            "previous_turn": {"question": question, "intent": {}, "sql": result["sql"]},
            "audit": audit,
            "user_trace": ["Answered with generated SQL (not checked against the data model)"],
            "debug_trace": [f"SQL fallback: answered, {len(rows)} row(s)"],
        }

    return node


def route_after_fallback(state: AgentState) -> str:
    return "unsupported" if state.get("unsupported") else "done"
