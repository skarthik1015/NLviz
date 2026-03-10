from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    build_chart_selector_node,
    build_executor_node,
    build_explainer_node,
    build_intent_mapper_node,
    build_sql_builder_node,
    build_validator_node,
    route_after_validator,
)
from app.agent.state import AgentState, build_initial_state
from app.connectors.base import DataConnector, SchemaContext
from app.models import SemanticIntent
from app.semantic import SemanticRegistry
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter


@dataclass(frozen=True)
class QueryGraphDependencies:
    connector: DataConnector
    schema_context: SchemaContext
    registry: SemanticRegistry
    intent_mapper: IntentMapperRouter
    intent_config: IntentMapperConfig


def build_query_graph(dependencies: QueryGraphDependencies):
    graph = StateGraph(AgentState)

    graph.add_node(
        "intent_mapper",
        build_intent_mapper_node(  # type: ignore[arg-type]
            dependencies.intent_mapper,
            dependencies.registry,
            dependencies.schema_context,
        ),
    )
    graph.add_node("sql_builder", build_sql_builder_node(dependencies.registry))  # type: ignore[arg-type]
    graph.add_node("executor", build_executor_node(dependencies.connector))  # type: ignore[arg-type]
    graph.add_node("validator", build_validator_node())  # type: ignore[arg-type]
    graph.add_node("chart_selector", build_chart_selector_node())  # type: ignore[arg-type]
    graph.add_node("explainer", build_explainer_node(dependencies.intent_config))  # type: ignore[arg-type]

    # Linear path to validator
    graph.add_edge(START, "intent_mapper")
    graph.add_edge("intent_mapper", "sql_builder")
    graph.add_edge("sql_builder", "executor")
    graph.add_edge("executor", "validator")

    # Validator: retry back to sql_builder or continue to chart_selector
    graph.add_conditional_edges(
        "validator",
        route_after_validator,  # type: ignore[arg-type]
        {"retry": "sql_builder", "continue": "chart_selector"},
    )

    graph.add_edge("chart_selector", "explainer")
    graph.add_edge("explainer", END)

    return graph.compile()


class QueryGraphRunner:
    def __init__(self, dependencies: QueryGraphDependencies):
        self._graph = build_query_graph(dependencies)

    def invoke(
        self,
        *,
        question: str,
        query_id: str,
        explicit_intent: SemanticIntent | None = None,
    ) -> AgentState:
        initial_state = build_initial_state(
            question=question,
            query_id=query_id,
            explicit_intent=explicit_intent,
        )
        return self._graph.invoke(initial_state)  # type: ignore[return-value]
