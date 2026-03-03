from __future__ import annotations

from pathlib import Path

from fastapi import Request

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.connectors.base import SchemaContext
from app.semantic import SemanticRegistry, load_semantic_registry
from app.services import FeedbackStore, IntentMapperConfig, IntentMapperRouter, QueryService


def build_runtime_services() -> dict[str, object]:
    schema_path = Path(__file__).resolve().parent / "semantic" / "schemas" / "ecommerce.yaml"
    if DuckDBConnector is None:
        raise RuntimeError("DuckDBConnector is unavailable. Install backend dependencies before starting the API.")
    connector = DuckDBConnector()
    schema_context = connector.get_schema()
    registry = load_semantic_registry(schema_path)
    intent_mapper = IntentMapperRouter(config=IntentMapperConfig.from_env())
    query_graph = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=schema_context,
            registry=registry,
            intent_mapper=intent_mapper,
        )
    )
    query_service = QueryService(query_graph=query_graph)
    feedback_store = FeedbackStore()
    return {
        "connector": connector,
        "schema_context": schema_context,
        "semantic_registry": registry,
        "query_graph": query_graph,
        "query_service": query_service,
        "feedback_store": feedback_store,
    }


def get_connector(request: Request) -> DuckDBConnector:
    return request.app.state.connector


def get_semantic_registry(request: Request) -> SemanticRegistry:
    return request.app.state.semantic_registry


def get_schema_context(request: Request) -> SchemaContext:
    return request.app.state.schema_context


def get_query_service(request: Request) -> QueryService:
    return request.app.state.query_service


def get_feedback_store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store
