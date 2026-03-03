from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.models import SemanticIntent

IntentMapper = Callable[[str], SemanticIntent]


def build_intent_mapper_node(intent_mapper: IntentMapper) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        explicit_intent = state.get("explicit_intent")
        if explicit_intent is not None:
            return {
                "intent": explicit_intent,
                "trace": [
                    f"Intent mapper: using explicit intent for metric '{explicit_intent.metric}'",
                ],
            }

        question = state.get("question", "").strip()
        if not question:
            raise ValueError("Question is required before intent mapping")

        intent = intent_mapper(question)
        return {
            "intent": intent,
            "trace": [
                f"Intent mapper: selected metric '{intent.metric}'",
                "Intent mapper: resolved dimensions "
                + (", ".join(intent.dimensions) if intent.dimensions else "none"),
            ],
        }

    return node
