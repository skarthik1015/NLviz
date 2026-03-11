from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from app.agent.graph import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.connectors.base import DataConnector, SchemaContext
from app.semantic import SemanticRegistry, load_semantic_registry
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionResolutionError, ConnectionRuntime, ConnectionService
from app.services.connection_store import ConnectionStore
from app.services.feedback_store import FeedbackStore
from app.services.generation_job_manager import GenerationJobManager
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter
from app.services.query_service import QueryService
from app.services.secret_store import SecretStore

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def build_default_runtime(intent_config: IntentMapperConfig | None = None) -> ConnectionRuntime:
    """Build the default ecommerce runtime (backward compat)."""
    schema_path = Path(__file__).resolve().parent / "semantic" / "schemas" / "ecommerce.yaml"
    connector = DuckDBConnector()
    schema_context = connector.get_schema()
    registry = load_semantic_registry(schema_path)
    config = intent_config or IntentMapperConfig.from_env()
    intent_mapper = IntentMapperRouter(config=config)
    query_graph = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=schema_context,
            registry=registry,
            intent_mapper=intent_mapper,
            intent_config=config,
        )
    )
    query_service = QueryService(query_graph=query_graph)
    return ConnectionRuntime(
        connector=connector,
        schema_context=schema_context,
        registry=registry,
        query_graph=query_graph,
        query_service=query_service,
    )


def build_all_services() -> dict[str, Any]:
    """Build all services for app startup."""
    intent_config = IntentMapperConfig.from_env()
    connection_store = ConnectionStore()
    secret_store = SecretStore()
    connection_service = ConnectionService(
        connection_store=connection_store,
        secret_store=secret_store,
        intent_config=intent_config,
    )

    # Register default ecommerce runtime
    default_runtime = build_default_runtime(intent_config)
    connection_service.register_runtime("default", default_runtime)

    job_manager = GenerationJobManager(connection_store=connection_store)
    audit_log = AuditLog()
    feedback_store = FeedbackStore()

    return {
        "connection_service": connection_service,
        "connection_store": connection_store,
        "secret_store": secret_store,
        "job_manager": job_manager,
        "audit_log": audit_log,
        "feedback_store": feedback_store,
        "intent_config": intent_config,
        # Backward compat: expose default runtime's services directly
        "connector": default_runtime.connector,
        "schema_context": default_runtime.schema_context,
        "semantic_registry": default_runtime.registry,
        "query_graph": default_runtime.query_graph,
        "query_service": default_runtime.query_service,
    }


# ── Dependency injection helpers ─────────────────────────────────────


def _resolve_connection_id(request: Request) -> str:
    """Extract and validate connection_id from header, defaulting to 'default'."""
    raw = request.headers.get("X-Connection-Id")
    if not raw:
        return "default"
    if raw == "default" or _UUID4_RE.match(raw):
        return raw
    raise HTTPException(status_code=400, detail="INVALID_CONNECTION_ID")


def get_connection_service(request: Request) -> ConnectionService:
    return request.app.state.connection_service


def get_connection_store(request: Request) -> ConnectionStore:
    return request.app.state.connection_store


def get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store


def get_job_manager(request: Request) -> GenerationJobManager:
    return request.app.state.job_manager


def get_audit_log(request: Request) -> AuditLog:
    return request.app.state.audit_log


def _get_runtime(request: Request) -> ConnectionRuntime:
    conn_id = _resolve_connection_id(request)
    try:
        return request.app.state.connection_service.get_runtime(conn_id)
    except ConnectionResolutionError:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")


def get_connector(request: Request) -> DataConnector:
    return _get_runtime(request).connector


def get_semantic_registry(request: Request) -> SemanticRegistry:
    return _get_runtime(request).registry


def get_schema_context(request: Request) -> SchemaContext:
    return _get_runtime(request).schema_context


def get_query_service(request: Request) -> QueryService:
    return _get_runtime(request).query_service


def get_feedback_store(request: Request) -> FeedbackStore:
    return request.app.state.feedback_store
