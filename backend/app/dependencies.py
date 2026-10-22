from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from app.agent.checkpoint import build_checkpointer
from app.agent.graph import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.connectors.base import DataConnector, SchemaContext
from app.connectors.jammi_connector import JammiConnector
from app.jammi.client import JammiClient, JammiSettings
from app.semantic import SemanticRegistry, load_semantic_registry
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionResolutionError, ConnectionRuntime, ConnectionService
from app.services.connection_store import BaseConnectionStore
from app.services.feedback_store import BaseFeedbackStore
from app.services.generation_job_manager import GenerationJobManager
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter
from app.services.jammi_sources import JammiSourceProvisioner, SourceSpec
from app.grounding.service import GroundingService
from app.services.jammi_uploads import LocalUploadStore, ManagedUploadStore, S3UploadStore
from app.services.query_service import QueryService
from app.services.secret_store import BaseSecretStore
from app.security.auth import AuthUser, get_current_user
from app.storage.s3_storage import S3Storage

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.storage.db_pool import DatabasePool
    from app.storage.schema_storage import BaseSchemaStorage

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


logger = logging.getLogger(__name__)

# The bundled Olist demo is one shared, read-only connection served to every user.
DEMO_WORKSPACE_ID = "demo"
DEMO_CONNECTION_ID = "default"


def build_demo_jammi_connector(
    client: JammiClient, demo_data_url: str, tables: list[str], *, workspace_id: str = DEMO_WORKSPACE_ID
) -> JammiConnector:
    """Register the bundled Olist Parquet files as Jammi sources (idempotent across restarts)."""
    specs = [
        SourceSpec(name=table, url=f"{demo_data_url.rstrip('/')}/{table}.parquet", format="parquet", tables=[table])
        for table in tables
    ]
    provisioned = JammiSourceProvisioner(client).provision(
        workspace_id=workspace_id,
        connection_id=DEMO_CONNECTION_ID,
        specs=specs,
        reuse_existing=True,
    )
    return JammiConnector(client, provisioned.tables)


def connect_jammi(settings: JammiSettings, *, wait_s: float = 60.0, connect=JammiClient) -> JammiClient:
    """Connect, retrying while jammi-server starts (Compose starts both together). Still fails
    after wait_s, rather than silently serving from the legacy engine."""
    deadline = time.monotonic() + wait_s
    delay = 1.0
    while True:
        try:
            return connect(settings)
        except Exception as exc:
            if time.monotonic() + delay > deadline:
                raise
            logger.info("jammi-server not reachable yet (%s); retrying in %.0fs", type(exc).__name__, delay)
            time.sleep(delay)
            delay = min(delay * 2, 8.0)


def build_jammi_upload_store(config: AppConfig, upload_storage: S3Storage | None) -> ManagedUploadStore | None:
    """Where uploads served by Jammi live: the upload bucket when configured, else a local
    directory shared with jammi-server. None: uploads stay on the legacy DuckDB path."""
    if upload_storage is not None:
        return S3UploadStore(upload_storage)
    if config.jammi_upload_dir and config.jammi_upload_url:
        return LocalUploadStore(Path(config.jammi_upload_dir), config.jammi_upload_url)
    logger.warning("Jammi is configured but no upload storage is: uploads use the legacy DuckDB path")
    return None


def build_default_runtime(
    intent_config: IntentMapperConfig | None = None,
    *,
    jammi_client: JammiClient | None = None,
    demo_data_url: str | None = None,
    grounding: GroundingService | None = None,
    demo_workspace_id: str = DEMO_WORKSPACE_ID,
    checkpointer=None,
) -> ConnectionRuntime:
    """Build the default ecommerce runtime: on Jammi when configured, else the bundled DuckDB file."""
    schema_path = Path(__file__).resolve().parent / "semantic" / "schemas" / "ecommerce.yaml"
    registry = load_semantic_registry(schema_path)
    connector: DataConnector
    if jammi_client is not None and demo_data_url:
        connector = build_demo_jammi_connector(
            jammi_client, demo_data_url, [table.name for table in registry.schema.tables],
            workspace_id=demo_workspace_id,
        )
    else:
        connector = DuckDBConnector()
    schema_context = connector.get_schema()
    config = intent_config or IntentMapperConfig.from_env()
    value_index = example_index = None
    if grounding is not None and isinstance(connector, JammiConnector):
        grounding.ensure(connection_id=DEMO_CONNECTION_ID, workspace_id=DEMO_WORKSPACE_ID,
                         connector=connector, registry=registry)
        value_index = grounding.lookup(DEMO_CONNECTION_ID)
        example_index = grounding.example_lookup(DEMO_CONNECTION_ID)
    intent_mapper = IntentMapperRouter(config=config, value_index=value_index, example_index=example_index)
    query_graph = QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=schema_context,
            registry=registry,
            intent_mapper=intent_mapper,
            intent_config=config,
            table_ref=connector.table_ref_resolver(),
        ),
        checkpointer=checkpointer,
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
    upload_storage: S3Storage | None = None,
) -> dict[str, Any]:
    """Build all services for app startup.

    Uses factory classmethods on each store so the correct implementation
    (local vs AWS) is chosen automatically from *config*.
    """
    intent_config = IntentMapperConfig.from_env()
    checkpointer = build_checkpointer(config.database_url)

    # Jammi: connect at startup and fail fast when configured but unreachable, rather than
    # silently serving connections from the legacy engine.
    jammi_settings = JammiSettings.from_config(config)
    if config.environment == "production" and (jammi_settings is None or not config.jammi_demo_data_url):
        # DuckDB only serves the bundled demo offline in development; in production every query,
        # the demo's included, runs on Jammi.
        raise RuntimeError("JAMMI_URL, JAMMI_SERVICE_TENANT and JAMMI_DEMO_DATA_URL are required in production")
    jammi_client = connect_jammi(jammi_settings) if jammi_settings else None
    jammi_provisioner = JammiSourceProvisioner(jammi_client) if jammi_client else None

    connection_store = BaseConnectionStore.create(config, pool=pool)
    secret_store = BaseSecretStore.create(config)
    feedback_store = BaseFeedbackStore.create(config, pool=pool)

    jammi_upload_store = build_jammi_upload_store(config, upload_storage) if jammi_client else None
    # Grounding needs Jammi and a place jammi-server can read the value corpora from.
    grounding = (
        GroundingService(jammi_client, jammi_upload_store, examples_enabled=config.examples_enabled)
        if jammi_client and jammi_upload_store and config.grounding_enabled
        else None
    )

    connection_service = ConnectionService(
        connection_store=connection_store,
        secret_store=secret_store,
        intent_config=intent_config,
        schema_storage=schema_storage,
        jammi_client=jammi_client,
        grounding=grounding,
        checkpointer=checkpointer.saver,
    )

    # Register default ecommerce runtime
    default_runtime = build_default_runtime(
        intent_config, jammi_client=jammi_client, demo_data_url=config.jammi_demo_data_url, grounding=grounding,
        checkpointer=checkpointer.saver,
    )
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
        "jammi_provisioner": jammi_provisioner,
        "jammi_upload_store": jammi_upload_store,
        "grounding": grounding,
        "checkpointer": checkpointer,
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


def get_jammi_provisioner(request: Request) -> JammiSourceProvisioner | None:
    """None when Jammi is not configured: new connections use the legacy connectors."""
    return getattr(request.app.state, "jammi_provisioner", None)


@dataclass(frozen=True)
class MySQLSettings:
    federation: bool = False
    snapshot_max_rows: int = 5_000_000


def get_mysql_settings(request: Request) -> MySQLSettings:
    config = getattr(request.app.state, "config", None)
    if config is None:
        return MySQLSettings()
    return MySQLSettings(federation=config.mysql_federation, snapshot_max_rows=config.mysql_snapshot_max_rows)


def get_s3_import_allow_custom_endpoint(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    return bool(config and config.s3_import_allow_custom_endpoint)


def get_grounding(request: Request) -> GroundingService | None:
    return getattr(request.app.state, "grounding", None)


def get_jammi_upload_store(request: Request) -> ManagedUploadStore | None:
    """None when uploads are not served through Jammi."""
    return getattr(request.app.state, "jammi_upload_store", None)


def get_schema_storage(request: Request):
    return getattr(request.app.state, "schema_storage", None)


def _ensure_connection_access(request: Request, connection_id: str, user: AuthUser) -> None:
    """Only the owner may query a connection (README item 2; decision AD-5).

    A non-owner gets 404, not 403, so connection ids cannot be probed for existence. The bundled
    "default" demo connection is shared. Profiles without an owner predate per-user ownership and
    keep the same rule as the connection routes' _verify_owner.
    """
    if connection_id == "default":
        return
    profile = request.app.state.connection_store.get_connection(connection_id)
    if profile is not None and profile.owner_id is not None and profile.owner_id != user.user_id:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")


def _get_runtime(request: Request, user: AuthUser) -> ConnectionRuntime:
    conn_id = _resolve_connection_id(request)
    _ensure_connection_access(request, conn_id, user)
    try:
        return request.app.state.connection_service.get_runtime(conn_id)
    except ConnectionResolutionError as exc:
        if exc.reason == "not found":
            raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND") from exc
        if exc.reason == "archived":
            raise HTTPException(status_code=410, detail="CONNECTION_ARCHIVED") from exc
        if exc.reason.startswith("published schema file is missing"):
            raise HTTPException(status_code=409, detail="SCHEMA_MISSING") from exc
        if exc.reason == "no published schema — generate and publish a schema first":
            raise HTTPException(status_code=409, detail="SCHEMA_NOT_PUBLISHED") from exc
        raise HTTPException(status_code=400, detail="CONNECTION_NOT_READY") from exc


# The user arrives through Depends so dependency_overrides apply and FastAPI resolves it once per
# request alongside the route's own get_current_user.


def get_connector(request: Request, user: AuthUser = Depends(get_current_user)) -> DataConnector:
    return _get_runtime(request, user).connector


def get_semantic_registry(request: Request, user: AuthUser = Depends(get_current_user)) -> SemanticRegistry:
    return _get_runtime(request, user).registry


def get_schema_context(request: Request, user: AuthUser = Depends(get_current_user)) -> SchemaContext:
    return _get_runtime(request, user).schema_context


def get_query_service(request: Request, user: AuthUser = Depends(get_current_user)) -> QueryService:
    return _get_runtime(request, user).query_service


def get_conversation_scope(request: Request, user: AuthUser = Depends(get_current_user)) -> str:
    """Namespace for conversation threads: a user's conversations on one connection."""
    return f"{user.user_id}/{_resolve_connection_id(request)}"


def get_feedback_store(request: Request) -> BaseFeedbackStore:
    return request.app.state.feedback_store
