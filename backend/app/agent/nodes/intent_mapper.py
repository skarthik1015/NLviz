from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.connectors.base import SchemaContext
from app.semantic import SemanticRegistry
from app.services.intent_mapper import IntentMapperRouter, validate_semantic_intent


def build_intent_mapper_node(
    intent_mapper: IntentMapperRouter,
    registry: SemanticRegistry,
    schema: SchemaContext,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        explicit_intent = state.get("explicit_intent")
        if explicit_intent is not None:
            validated = validate_semantic_intent(explicit_intent, registry, schema)
            dims = ", ".join(validated.dimensions) if validated.dimensions else "none"
            return {
                "intent": validated,
                "intent_source": "explicit",
                "user_trace": [
                    f"Understood: {validated.metric.replace('_', ' ')}"
                    + (f" by {dims.replace('_', ' ')}" if validated.dimensions else "")
                ],
                "debug_trace": [f"Intent mapper: source=explicit, metric={validated.metric}, dims={dims}"],
            }

        question = state.get("question", "").strip()
        if not question:
            raise ValueError("Question is required before intent mapping")

        result = intent_mapper.map_with_metadata(question, registry, schema)
        dims = ", ".join(result.intent.dimensions) if result.intent.dimensions else "none"
        return {
            "intent": result.intent,
            "intent_source": result.source,
            "user_trace": [
                f"Understood: {result.intent.metric.replace('_', ' ')}"
                + (f" by {dims.replace('_', ' ')}" if result.intent.dimensions else "")
            ],
            "debug_trace": result.trace,
        }

    return node
