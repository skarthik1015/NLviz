from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from app.agent.graph import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.connectors.base import DataConnector, SchemaContext
from app.semantic import SemanticRegistry, load_semantic_registry
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionResolutionError, ConnectionRuntime, ConnectionService
from app.services.connection_store import BaseConnectionStore
from app.services.feedback_store import BaseFeedbackStore
from app.services.generation_job_manager import GenerationJobManager
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter
from app.services.query_service import QueryService
from app.services.secret_store import BaseSecretStore
from app.storage.s3_storage import S3Storage

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.storage.db_pool import DatabasePool
    from app.storage.schema_storage import BaseSchemaStorage

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


def build_all_services(
    config: "AppConfig",
    pool: "DatabasePool | None" = None,
    schema_storage: "BaseSchemaStorage | None" = None,
) -> dict[str, Any]:
    """Build all services for app startup.

    Uses factory classmethods on each store so the correct implementation
    (local vs AWS) is chosen automatically from *config*.
    """
    intent_config = IntentMapperConfig.from_env()

    connection_store = BaseConnectionStore.create(config, pool=pool)
    secret_store = BaseSecretStore.create(config)
    feedback_store = BaseFeedbackStore.create(config, pool=pool)

    connection_service = ConnectionService(
        connection_store=connection_store,
        secret_store=secret_store,
        intent_config=intent_config,
        schema_storage=schema_storage,
    )

    # Register default ecommerce runtime
    default_runtime = build_default_runtime(intent_config)
    connection_service.register_runtime("default", default_runtime)

    job_manager = GenerationJobManager(
        connection_store=connection_store,
        schema_storage=schema_storage,
    )
    audit_log = AuditLog()

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


def get_connection_store(request: Request) -> BaseConnectionStore:
    return request.app.state.connection_store


def get_secret_store(request: Request) -> BaseSecretStore:
    return request.app.state.secret_store


def get_job_manager(request: Request) -> GenerationJobManager:
    return request.app.state.job_manager


def get_audit_log(request: Request) -> AuditLog:
    return request.app.state.audit_log


def get_upload_storage(request: Request) -> S3Storage | None:
    """Return the S3 upload storage, or None in local-dev mode."""
    return getattr(request.app.state, "upload_storage", None)


def get_schema_storage(request: Request):
    return getattr(request.app.state, "schema_storage", None)


def _get_runtime(request: Request) -> ConnectionRuntime:
    conn_id = _resolve_connection_id(request)
    try:
        return request.app.state.connection_service.get_runtime(conn_id)
    except ConnectionResolutionError as exc:
        if exc.reason == "not found":
            raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND") from exc
        if exc.reason == "archived":
            raise HTTPException(status_code=410, detail="CONNECTION_ARCHIVED") from exc
        if exc.reason == "no published schema — generate and publish a schema first":
            raise HTTPException(status_code=409, detail="SCHEMA_NOT_PUBLISHED") from exc
        raise HTTPException(status_code=400, detail="CONNECTION_NOT_READY") from exc


def get_connector(request: Request) -> DataConnector:
    return _get_runtime(request).connector


def get_semantic_registry(request: Request) -> SemanticRegistry:
    return _get_runtime(request).registry


def get_schema_context(request: Request) -> SchemaContext:
    return _get_runtime(request).schema_context


def get_query_service(request: Request) -> QueryService:
    return _get_runtime(request).query_service


def get_feedback_store(request: Request) -> BaseFeedbackStore:
    return request.app.state.feedback_store
