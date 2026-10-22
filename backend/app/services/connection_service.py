"""Per-request connection routing and runtime management."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from app.agent.graph import QueryGraphDependencies, QueryGraphRunner


class ConnectionResolutionError(Exception):
    """Raised when a connection_id cannot be resolved to an active runtime."""

    def __init__(self, connection_id: str, reason: str) -> None:
        self.connection_id = connection_id
        self.reason = reason
        super().__init__(f"Cannot resolve connection '{connection_id}': {reason}")
from app.connectors.base import DataConnector, SchemaContext
from app.connectors.jammi_connector import JammiConnector
from app.services.jammi_sources import database_source_ids
from app.models.connection import ConnectionProfile
from app.semantic import SemanticRegistry, load_semantic_registry, load_semantic_registry_from_yaml
from app.services.connection_store import BaseConnectionStore
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter
from app.services.definitions import DefinitionStore
from app.services.query_service import QueryService
from app.services.secret_store import BaseSecretStore

if TYPE_CHECKING:
    from app.grounding.service import GroundingService
    from app.jammi.client import JammiClient
    from app.storage.schema_storage import BaseSchemaStorage

logger = logging.getLogger(__name__)


@dataclass
class ConnectionRuntime:
    """All services needed to run queries for one connection."""

    connector: DataConnector
    schema_context: SchemaContext
    registry: SemanticRegistry
    query_graph: QueryGraphRunner
    query_service: QueryService


class ConnectionService:
    """Manages ConnectionRuntime instances, keyed by connection_id."""

    def __init__(
        self,
        connection_store: BaseConnectionStore,
        secret_store: BaseSecretStore,
        intent_config: IntentMapperConfig | None = None,
        schema_storage: "BaseSchemaStorage | None" = None,
        jammi_client: "JammiClient | None" = None,
        grounding: "GroundingService | None" = None,
        checkpointer=None,
    ):
        self._store = connection_store
        self._grounding = grounding
        # Shared conversation store (app.agent.checkpoint); None gives each runtime its own memory.
        self._checkpointer = checkpointer
        self._secrets = secret_store
        self._jammi_client = jammi_client
        self._intent_config = intent_config or IntentMapperConfig.from_env()
        self._schema_storage = schema_storage
        self._runtimes: dict[str, ConnectionRuntime] = {}
        # Bumped by every publish and eviction: a runtime built before the bump is stale and is not
        # installed (review B13: a cold load finishing after a delete reinstalled the old runtime).
        self._revisions: dict[str, int] = {}
        self._publish_locks: dict[str, Lock] = {}
        self._lock = Lock()

    def _revision(self, connection_id: str) -> int:
        with self._lock:
            return self._revisions.get(connection_id, 0)

    def get_runtime(self, connection_id: str) -> ConnectionRuntime:
        """Get or lazily build a runtime for a connection."""
        with self._lock:
            if connection_id in self._runtimes:
                return self._runtimes[connection_id]
            revision = self._revisions.get(connection_id, 0)

        # Build outside lock to avoid holding it during IO
        runtime = self._load_runtime(connection_id)
        with self._lock:
            if connection_id in self._runtimes:  # another request built it meanwhile
                return self._runtimes[connection_id]
            if self._revisions.get(connection_id, 0) == revision:
                self._runtimes[connection_id] = runtime
            # Otherwise published or deleted meanwhile: answer this request, cache nothing stale.
            return runtime

    def register_runtime(self, connection_id: str, runtime: ConnectionRuntime) -> None:
        """Register a pre-built runtime (used for default ecommerce)."""
        with self._lock:
            self._runtimes[connection_id] = runtime

    def activate_schema(self, connection_id: str, version_id: str) -> None:
        """Promote a schema version to published and rebuild the runtime."""
        version = self._store.get_version(version_id)
        if version is None:
            raise ValueError(f"Schema version '{version_id}' not found")
        if version.connection_id != connection_id:
            raise ValueError("Version does not belong to this connection")
        if version.status != "validated":
            raise ValueError(f"Cannot publish version in status '{version.status}'; must be 'validated'")
        profile = self._store.get_connection(connection_id)
        if profile is None:
            raise ValueError(f"Connection '{connection_id}' not found")

        with self._lock:
            publish_lock = self._publish_locks.setdefault(connection_id, Lock())
        with publish_lock:
            # Prepare first: a schema that cannot load (missing file, bad YAML) fails here, before
            # anything durable changes. Recording it as published first left restarts unable to
            # load the connection (review B12).
            runtime = self._build_runtime(self._build_connector(profile), version.schema_path, profile)
            self._store.publish_version(connection_id, version_id)
            with self._lock:
                self._revisions[connection_id] = self._revisions.get(connection_id, 0) + 1
                old = self._runtimes.get(connection_id)
                self._runtimes[connection_id] = runtime
            if old and hasattr(old.connector, "close"):
                old.connector.close()

    def evict_runtime(self, connection_id: str) -> None:
        """Remove a runtime from cache (e.g., on connection deletion)."""
        with self._lock:
            self._revisions[connection_id] = self._revisions.get(connection_id, 0) + 1
            old = self._runtimes.pop(connection_id, None)
        if old and hasattr(old.connector, "close"):
            old.connector.close()

    def has_runtime(self, connection_id: str) -> bool:
        with self._lock:
            return connection_id in self._runtimes

    # ── Internal ─────────────────────────────────────────────────────

    def _load_runtime(self, connection_id: str) -> ConnectionRuntime:
        """Load a runtime from persisted connection + published schema."""
        profile = self._store.get_connection(connection_id)
        if profile is None:
            raise ConnectionResolutionError(connection_id, "not found")
        if profile.status != "active":
            raise ConnectionResolutionError(connection_id, "archived")

        version = self._store.get_published_version(connection_id)
        if version is None:
            raise ConnectionResolutionError(
                connection_id,
                "no published schema — generate and publish a schema first",
            )

        connector = self._build_connector(profile)
        return self._build_runtime(connector, version.schema_path, profile)

    def build_connector(self, profile: ConnectionProfile) -> DataConnector:
        return self._build_connector(profile)

    def _stored(self, profile: ConnectionProfile, key: str) -> list:
        """What was read from the source database when the connection was created, stored with its
        parameters like snapshot file URLs: foreign keys ("declared_joins") and columns left out of
        the data model ("hidden_columns", J36). Uploads have neither."""
        if profile.connector_type not in ("postgres", "mysql"):
            return []
        try:
            return list((self._secrets.get_by_connection_id(profile.connection_id) or {}).get(key) or [])
        except Exception:
            logger.warning("Could not read %s for %s", key, profile.connection_id, exc_info=True)
            return []

    def _build_connector(self, profile: ConnectionProfile) -> DataConnector:
        """Instantiate a connector from a profile's stored credentials."""
        if profile.engine == "jammi":
            if self._jammi_client is None:
                raise ConnectionResolutionError(profile.connection_id, "jammi engine not configured")
            return JammiConnector(
                self._jammi_client,
                profile.jammi_tables,
                denied_columns=profile.denied_columns or [],
                database_sources=database_source_ids(profile.workspace_id, profile.connection_id, profile.jammi_tables),
                declared_joins=self._stored(profile, "declared_joins"),
                hidden_columns=self._stored(profile, "hidden_columns"),
            )

        # Connections created before Jammi (v1 engine) are not served any more (Phase 1g).
        raise ConnectionResolutionError(
            profile.connection_id, "this connection was created before the Jammi engine; recreate it"
        )

    def _build_runtime(
        self, connector: DataConnector, schema_path: str, profile: ConnectionProfile | None = None
    ) -> ConnectionRuntime:
        """Build a full runtime stack from connector + schema path."""
        schema_context = connector.get_schema()
        try:
            if self._schema_storage is not None:
                yaml_content = self._schema_storage.load(schema_path)
                registry = load_semantic_registry_from_yaml(yaml_content)
            else:
                registry = load_semantic_registry(schema_path)
        except FileNotFoundError as exc:
            # The app DB points at a schema file that is gone (e.g. storage lost in a redeploy): say
            # so instead of failing every request with a 500.
            raise ConnectionResolutionError(
                profile.connection_id if profile else "default",
                "published schema file is missing — generate and publish the schema again",
            ) from exc
        value_index = example_index = None
        if self._grounding is not None and profile is not None and profile.engine == "jammi" and profile.workspace_id:
            # Build (or reuse) the value index in the background; grounding switches on when ready.
            self._grounding.ensure(connection_id=profile.connection_id, workspace_id=profile.workspace_id,
                                   connector=connector, registry=registry, schema_key=schema_path)
            value_index = self._grounding.lookup(profile.connection_id)
            example_index = self._grounding.example_lookup(profile.connection_id)
        intent_mapper = IntentMapperRouter(config=self._intent_config, value_index=value_index,
                                           example_index=example_index)
        query_graph = QueryGraphRunner(
            QueryGraphDependencies(
                connector=connector,
                schema_context=schema_context,
                registry=registry,
                intent_mapper=intent_mapper,
                intent_config=self._intent_config,
                table_ref=connector.table_ref_resolver(),
                definitions=DefinitionStore(self._schema_storage, profile.connection_id) if profile is not None else None,
            ),
            checkpointer=self._checkpointer,
        )
        query_service = QueryService(query_graph=query_graph)
        return ConnectionRuntime(
            connector=connector,
            schema_context=schema_context,
            registry=registry,
            query_graph=query_graph,
            query_service=query_service,
        )
