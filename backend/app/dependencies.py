from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.connectors import DuckDBConnector
from app.semantic import SemanticRegistry, load_semantic_registry
from app.services import FeedbackStore, QueryService


@lru_cache(maxsize=1)
def get_connector() -> DuckDBConnector:
    return DuckDBConnector()


@lru_cache(maxsize=1)
def get_semantic_registry() -> SemanticRegistry:
    schema_path = Path(__file__).resolve().parent / "semantic" / "schemas" / "ecommerce.yaml"
    return load_semantic_registry(schema_path)


@lru_cache(maxsize=1)
def get_query_service() -> QueryService:
    return QueryService(connector=get_connector(), registry=get_semantic_registry())


@lru_cache(maxsize=1)
def get_feedback_store() -> FeedbackStore:
    return FeedbackStore()
