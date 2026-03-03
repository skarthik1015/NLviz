from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.connectors.base import SchemaContext
from app.semantic import SemanticRegistry
from app.services import IntentMapperRouter, validate_semantic_intent


def build_intent_mapper_node(
    intent_mapper: IntentMapperRouter,
    registry: SemanticRegistry,
    schema: SchemaContext,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        explicit_intent = state.get("explicit_intent")
        if explicit_intent is not None:
            validated = validate_semantic_intent(explicit_intent, registry, schema)
            return {
                "intent": validated,
                "intent_source": "explicit",
                "trace": [
                    f"Intent mapper: using explicit intent for metric '{validated.metric}'",
                ],
            }

        question = state.get("question", "").strip()
        if not question:
            raise ValueError("Question is required before intent mapping")

        result = intent_mapper.map_with_metadata(question, registry, schema)
        return {
            "intent": result.intent,
            "intent_source": result.source,
            "trace": result.trace,
        }

    return node
