from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import build_executor_node, build_intent_mapper_node, build_sql_builder_node
from app.agent.state import AgentState, build_initial_state
from app.connectors.base import DataConnector, SchemaContext
from app.models import SemanticIntent
from app.semantic import SemanticRegistry
from app.services import IntentMapperRouter


@dataclass(frozen=True)
class QueryGraphDependencies:
    connector: DataConnector
    schema_context: SchemaContext
    registry: SemanticRegistry
    intent_mapper: IntentMapperRouter


def build_query_graph(dependencies: QueryGraphDependencies):
    graph = StateGraph(AgentState)
    graph.add_node(
        "intent_mapper",
        cast(
            object,
            build_intent_mapper_node(
                dependencies.intent_mapper,
                dependencies.registry,
                dependencies.schema_context,
            ),
        ),
    )
    graph.add_node("sql_builder", cast(object, build_sql_builder_node(dependencies.registry)))
    graph.add_node("executor", cast(object, build_executor_node(dependencies.connector)))

    graph.add_edge(START, "intent_mapper")
    graph.add_edge("intent_mapper", "sql_builder")
    graph.add_edge("sql_builder", "executor")
    graph.add_edge("executor", END)

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
        return cast(AgentState, self._graph.invoke(initial_state))
