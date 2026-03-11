"""Per-request connection routing and runtime management."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from app.agent.graph import QueryGraphDependencies, QueryGraphRunner


class ConnectionResolutionError(Exception):
    """Raised when a connection_id cannot be resolved to an active runtime."""

    def __init__(self, connection_id: str, reason: str) -> None:
        self.connection_id = connection_id
        self.reason = reason
        super().__init__(f"Cannot resolve connection '{connection_id}': {reason}")
from app.connectors import CONNECTOR_REGISTRY, DuckDBConnector
from app.connectors.base import DataConnector, SchemaContext
from app.models.connection import ConnectionProfile
from app.semantic import SemanticRegistry, load_semantic_registry
from app.services.connection_store import ConnectionStore
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter
from app.services.query_service import QueryService
from app.services.secret_store import SecretStore

if TYPE_CHECKING:
    pass

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
        connection_store: ConnectionStore,
        secret_store: SecretStore,
        intent_config: IntentMapperConfig | None = None,
    ):
        self._store = connection_store
        self._secrets = secret_store
        self._intent_config = intent_config or IntentMapperConfig.from_env()
        self._runtimes: dict[str, ConnectionRuntime] = {}
        self._lock = Lock()

    def get_runtime(self, connection_id: str) -> ConnectionRuntime:
        """Get or lazily build a runtime for a connection."""
        with self._lock:
            if connection_id in self._runtimes:
                return self._runtimes[connection_id]

        # Build outside lock to avoid holding it during IO
        runtime = self._load_runtime(connection_id)
        with self._lock:
            # Double-check after reacquiring
            if connection_id not in self._runtimes:
                self._runtimes[connection_id] = runtime
            return self._runtimes[connection_id]

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

        # Archive any previously published version for this connection
        for v in self._store.list_versions(connection_id):
            if v.status == "published" and v.version_id != version_id:
                self._store.update_version(v.model_copy(update={"status": "archived"}))

        # Publish
        self._store.update_version(version.model_copy(update={"status": "published"}))

        # Rebuild runtime
        profile = self._store.get_connection(connection_id)
        if profile is None:
            raise ValueError(f"Connection '{connection_id}' not found")

        connector = self._build_connector(profile)
        schema_path = Path(version.schema_path)
        runtime = self._build_runtime(connector, schema_path)

        with self._lock:
            old = self._runtimes.get(connection_id)
            self._runtimes[connection_id] = runtime
            if old and hasattr(old.connector, "close"):
                old.connector.close()

    def evict_runtime(self, connection_id: str) -> None:
        """Remove a runtime from cache (e.g., on connection deletion)."""
        with self._lock:
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
        return self._build_runtime(connector, Path(version.schema_path))

    def _build_connector(self, profile: ConnectionProfile) -> DataConnector:
        """Instantiate a connector from a profile's stored credentials."""
        connector_cls = CONNECTOR_REGISTRY.get(profile.connector_type)
        if connector_cls is None:
            raise ValueError(f"Unsupported connector type: {profile.connector_type}")

        denied = profile.denied_columns or []

        if profile.connector_type == "duckdb":
            params = self._secrets.get_by_connection_id(profile.connection_id)
            return DuckDBConnector(db_path=params.get("db_path"), denied_columns=denied)

        # Postgres and others
        params = self._secrets.get_by_connection_id(profile.connection_id)
        return connector_cls(connection_params=params, denied_columns=denied)

    def _build_runtime(
        self, connector: DataConnector, schema_path: Path
    ) -> ConnectionRuntime:
        """Build a full runtime stack from connector + schema path."""
        schema_context = connector.get_schema()
        registry = load_semantic_registry(schema_path)
        intent_mapper = IntentMapperRouter(config=self._intent_config)
        query_graph = QueryGraphRunner(
            QueryGraphDependencies(
                connector=connector,
                schema_context=schema_context,
                registry=registry,
                intent_mapper=intent_mapper,
                intent_config=self._intent_config,
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
