from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.semantic import SemanticRegistry, build_sql_from_intent


def build_sql_builder_node(registry: SemanticRegistry) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        intent = state.get("intent")
        if intent is None:
            raise ValueError("Intent must be present before SQL compilation")

        # Apply self-correction from validator
        correction_hint = state.get("correction_hint")
        if correction_hint == "remove_date_filters":
            intent = intent.model_copy(update={"start_date": None, "end_date": None})
        elif correction_hint and correction_hint.startswith("reduce_limit:"):
            new_limit = int(correction_hint.split(":")[1])
            intent = intent.model_copy(update={"limit": new_limit})

        sql = build_sql_from_intent(intent, registry)
        return {
            "intent": intent,  # persist modified intent if correction was applied
            "sql": sql,
            "correction_hint": None,  # clear after applying
            "user_trace": ["SQL compiled from semantic model"],
            "debug_trace": [
                f"SQL builder: correction_hint={correction_hint!r}",
                f"SQL builder: {sql[:120].replace(chr(10), ' ')}{'...' if len(sql) > 120 else ''}",
            ],
        }

    return node
