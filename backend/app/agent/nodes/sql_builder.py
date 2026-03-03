from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.semantic import SemanticRegistry, build_sql_from_intent


def build_sql_builder_node(registry: SemanticRegistry) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        intent = state.get("intent")
        if intent is None:
            raise ValueError("Intent must be present before SQL compilation")

        sql = build_sql_from_intent(intent, registry)
        return {
            "sql": sql,
            "trace": ["SQL builder: compiled deterministic SQL from semantic registry"],
        }

    return node
