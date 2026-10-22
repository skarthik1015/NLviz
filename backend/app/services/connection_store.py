"""Connection and schema-version persistence.

Two concrete implementations share a common ABC:

- ``JsonlConnectionStore``:  Append-only JSONL files on local disk.
                             Used in development (no DATABASE_URL set).
- ``RDSConnectionStore``:    PostgreSQL via a shared ``DatabasePool``.
                             Used in production (DATABASE_URL set).

Factory::

    store = BaseConnectionStore.create(config, pool=pool)
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.storage.db_pool import DatabasePool

from app.models.connection import ConnectionProfile, SemanticSchemaVersion

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_CONNECTIONS_PATH = _DATA_DIR / "connections.jsonl"
_SCHEMA_VERSIONS_PATH = _DATA_DIR / "schema_versions.jsonl"


# ── Abstract Base ─────────────────────────────────────────────────────

class BaseConnectionStore(ABC):
    """Storage contract for connections and schema versions."""

    # ── Connection operations ─────────────────────────────────────────

    @abstractmethod
    def create_connection(self, profile: ConnectionProfile) -> ConnectionProfile: ...

    @abstractmethod
    def get_connection(self, connection_id: str) -> ConnectionProfile | None: ...

    @abstractmethod
    def list_connections(self, owner_id: str | None = None) -> list[ConnectionProfile]: ...

    @abstractmethod
    def archive_connection(self, connection_id: str) -> bool: ...

    @abstractmethod
    def update_jammi_tables(self, connection_id: str, jammi_tables: dict[str, str]) -> bool:
        """Replace a connection's table mapping (e.g. after a file is attached)."""

    # ── Schema version operations ─────────────────────────────────────

    @abstractmethod
    def create_version(self, version: SemanticSchemaVersion) -> SemanticSchemaVersion: ...

    @abstractmethod
    def get_version(self, version_id: str) -> SemanticSchemaVersion | None: ...

    @abstractmethod
    def update_version(self, version: SemanticSchemaVersion) -> SemanticSchemaVersion: ...

    @abstractmethod
    def get_published_version(self, connection_id: str) -> SemanticSchemaVersion | None: ...

    @abstractmethod
    def publish_version(self, connection_id: str, version_id: str) -> SemanticSchemaVersion:
        """Make a validated version the connection's only published one, archiving the previous, as
        one step: two concurrent publishes cannot both end up published (review B12)."""

    @abstractmethod
    def list_versions(self, connection_id: str) -> list[SemanticSchemaVersion]: ...

    @abstractmethod
    def archive_versions_for_connection(self, connection_id: str) -> None: ...

    @abstractmethod
    def get_published_connection_ids(self) -> set[str]: ...

    # ── Factory ───────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        config: "AppConfig",
        pool: "DatabasePool | None" = None,
    ) -> "BaseConnectionStore":
        """Return the appropriate implementation based on ``AppConfig``."""
        if config.database_url and pool is not None:
            logger.info("ConnectionStore: RDS (PostgreSQL)")
            return RDSConnectionStore(pool=pool)
        logger.info("ConnectionStore: local JSONL files")
        return JsonlConnectionStore()


# ── Backward-compat alias (existing code uses ConnectionStore directly) ──
# Remove once all callers have been updated to BaseConnectionStore.
ConnectionStore = None  # set at bottom of module after class definitions


# ── JSONL Implementation ──────────────────────────────────────────────

class JsonlConnectionStore(BaseConnectionStore):
    """Append-only JSONL file store with in-memory read cache.

    Each mutation appends a new record; on startup the file is replayed and
    later records overwrite earlier ones (last-write-wins per ID).
    """

    def __init__(
        self,
        connections_path: Path = _CONNECTIONS_PATH,
        versions_path: Path = _SCHEMA_VERSIONS_PATH,
    ) -> None:
        self._connections_path = connections_path
        self._versions_path = versions_path
        self._lock = Lock()
        self._connections: dict[str, ConnectionProfile] = {}
        self._versions: dict[str, SemanticSchemaVersion] = {}
        self._load()

    # ── Connection CRUD ──────────────────────────────────────────────

    def create_connection(self, profile: ConnectionProfile) -> ConnectionProfile:
        with self._lock:
            self._connections[profile.connection_id] = profile
            self._append_jsonl(self._connections_path, profile)
        return profile

    def get_connection(self, connection_id: str) -> ConnectionProfile | None:
        return self._connections.get(connection_id)

    def list_connections(self, owner_id: str | None = None) -> list[ConnectionProfile]:
        with self._lock:
            profiles = list(self._connections.values())
        if owner_id is not None:
            profiles = [p for p in profiles if p.owner_id == owner_id or p.owner_id is None]
        return [p for p in profiles if p.status == "active"]

    def archive_connection(self, connection_id: str) -> bool:
        with self._lock:
            profile = self._connections.get(connection_id)
            if profile is None:
                return False
            updated = profile.model_copy(update={"status": "archived"})
            self._connections[connection_id] = updated
            self._append_jsonl(self._connections_path, updated)
        return True

    def update_jammi_tables(self, connection_id: str, jammi_tables: dict[str, str]) -> bool:
        with self._lock:
            profile = self._connections.get(connection_id)
            if profile is None:
                return False
            updated = profile.model_copy(update={"jammi_tables": dict(jammi_tables)})
            self._connections[connection_id] = updated
            self._append_jsonl(self._connections_path, updated)
        return True

    # ── Schema Version CRUD ──────────────────────────────────────────

    def create_version(self, version: SemanticSchemaVersion) -> SemanticSchemaVersion:
        with self._lock:
            self._versions[version.version_id] = version
            self._append_jsonl(self._versions_path, version)
        return version

    def get_version(self, version_id: str) -> SemanticSchemaVersion | None:
        return self._versions.get(version_id)

    def update_version(self, version: SemanticSchemaVersion) -> SemanticSchemaVersion:
        with self._lock:
            self._versions[version.version_id] = version
            self._append_jsonl(self._versions_path, version)
        return version

    def publish_version(self, connection_id: str, version_id: str) -> SemanticSchemaVersion:
        with self._lock:
            version = self._versions.get(version_id)
            if version is None or version.connection_id != connection_id or version.status != "validated":
                raise ValueError("Version is not a validated version of this connection")
            for vid, v in list(self._versions.items()):
                if v.connection_id == connection_id and v.status == "published":
                    archived = v.model_copy(update={"status": "archived"})
                    self._versions[vid] = archived
                    self._append_jsonl(self._versions_path, archived)
            published = version.model_copy(update={"status": "published"})
            self._versions[version_id] = published
            self._append_jsonl(self._versions_path, published)
        return published

    def get_published_version(self, connection_id: str) -> SemanticSchemaVersion | None:
        with self._lock:
            for v in self._versions.values():
                if v.connection_id == connection_id and v.status == "published":
                    return v
        return None

    def list_versions(self, connection_id: str) -> list[SemanticSchemaVersion]:
        with self._lock:
            return [v for v in self._versions.values() if v.connection_id == connection_id]

    def archive_versions_for_connection(self, connection_id: str) -> None:
        with self._lock:
            for vid, v in list(self._versions.items()):
                if v.connection_id == connection_id and v.status != "archived":
                    updated = v.model_copy(update={"status": "archived"})
                    self._versions[vid] = updated
                    self._append_jsonl(self._versions_path, updated)

    def get_published_connection_ids(self) -> set[str]:
        with self._lock:
            return {v.connection_id for v in self._versions.values() if v.status == "published"}

    # ── Persistence helpers ──────────────────────────────────────────

    def _load(self) -> None:
        self._load_jsonl(self._connections_path, ConnectionProfile, self._connections, "connection_id")
        self._load_jsonl(self._versions_path, SemanticSchemaVersion, self._versions, "version_id")

    @staticmethod
    def _load_jsonl(path: Path, model_cls, target: dict, key_field: str) -> None:
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    record = model_cls.model_validate(json.loads(line))
                    target[getattr(record, key_field)] = record
        except Exception:
            logger.warning("Failed to load %s; starting with partial data", path, exc_info=True)

    @staticmethod
    def _append_jsonl(path: Path, record) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(record.model_dump_json() + "\n")
        except Exception:
            logger.warning("Failed to append to %s", path, exc_info=True)


# ── RDS Implementation ────────────────────────────────────────────────

class RDSConnectionStore(BaseConnectionStore):
    """PostgreSQL-backed store.

    All reads hit the DB directly (no in-memory cache) to ensure consistency
    across multiple ECS task instances.
    """

    def __init__(self, pool: "DatabasePool") -> None:
        self._pool = pool

    # ── Connection CRUD ──────────────────────────────────────────────

    def create_connection(self, profile: ConnectionProfile) -> ConnectionProfile:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO connections
                        (connection_id, display_name, connector_type, status,
                         denied_columns, owner_id, created_at,
                         engine, workspace_id, jammi_tables, mode, copied_at, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (connection_id) DO UPDATE SET
                        display_name   = EXCLUDED.display_name,
                        connector_type = EXCLUDED.connector_type,
                        status         = EXCLUDED.status,
                        denied_columns = EXCLUDED.denied_columns,
                        engine         = EXCLUDED.engine,
                        workspace_id   = EXCLUDED.workspace_id,
                        jammi_tables   = EXCLUDED.jammi_tables,
                        mode           = EXCLUDED.mode,
                        copied_at      = EXCLUDED.copied_at,
                        notes          = EXCLUDED.notes,
                        updated_at     = NOW()
                    """,
                    (
                        profile.connection_id,
                        profile.display_name,
                        profile.connector_type,
                        profile.status,
                        # JSONB parameters must be wrapped: psycopg2 sends a bare list as a
                        # Postgres ARRAY (an empty one happens to parse as '{}').
                        _jsonb(profile.denied_columns or []),
                        profile.owner_id,
                        profile.created_at,
                        profile.engine,
                        profile.workspace_id,
                        _jsonb(profile.jammi_tables),
                        profile.mode,
                        profile.copied_at,
                        _jsonb(profile.notes or []),
                    ),
                )
        return profile

    def get_connection(self, connection_id: str) -> ConnectionProfile | None:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM connections WHERE connection_id = %s",
                    (connection_id,),
                )
                row = cur.fetchone()
        return _row_to_connection(dict(row)) if row else None

    def list_connections(self, owner_id: str | None = None) -> list[ConnectionProfile]:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                if owner_id is not None:
                    cur.execute(
                        "SELECT * FROM connections WHERE status = 'active' AND (owner_id = %s OR owner_id IS NULL)",
                        (owner_id,),
                    )
                else:
                    cur.execute("SELECT * FROM connections WHERE status = 'active'")
                rows = cur.fetchall()
        return [_row_to_connection(dict(r)) for r in rows]

    def archive_connection(self, connection_id: str) -> bool:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE connections SET status = 'archived', updated_at = NOW() WHERE connection_id = %s",
                    (connection_id,),
                )
                return (cur.rowcount or 0) > 0

    def update_jammi_tables(self, connection_id: str, jammi_tables: dict[str, str]) -> bool:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE connections SET jammi_tables = %s, updated_at = NOW() WHERE connection_id = %s",
                    (_jsonb(dict(jammi_tables)), connection_id),
                )
                return (cur.rowcount or 0) > 0

    # ── Schema Version CRUD ──────────────────────────────────────────

    def create_version(self, version: SemanticSchemaVersion) -> SemanticSchemaVersion:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO schema_versions
                        (version_id, connection_id, status, schema_path,
                         validation_summary, generation_metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (version_id) DO NOTHING
                    """,
                    (
                        version.version_id,
                        version.connection_id,
                        version.status,
                        version.schema_path,
                        json.dumps(version.validation_summary.model_dump()) if version.validation_summary else None,
                        json.dumps(version.generation_metadata.model_dump()) if version.generation_metadata else None,
                        version.created_at,
                    ),
                )
        return version

    def get_version(self, version_id: str) -> SemanticSchemaVersion | None:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM schema_versions WHERE version_id = %s",
                    (version_id,),
                )
                row = cur.fetchone()
        return _row_to_version(dict(row)) if row else None

    def update_version(self, version: SemanticSchemaVersion) -> SemanticSchemaVersion:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE schema_versions SET
                        status              = %s,
                        schema_path         = %s,
                        validation_summary  = %s,
                        generation_metadata = %s,
                        updated_at          = NOW()
                    WHERE version_id = %s
                    """,
                    (
                        version.status,
                        version.schema_path,
                        json.dumps(version.validation_summary.model_dump()) if version.validation_summary else None,
                        json.dumps(version.generation_metadata.model_dump()) if version.generation_metadata else None,
                        version.version_id,
                    ),
                )
        return version

    def publish_version(self, connection_id: str, version_id: str) -> SemanticSchemaVersion:
        with self._pool.acquire() as conn:  # one transaction: committed on exit, rolled back on error
            with conn.cursor() as cur:
                # Serialises publishes of one connection.
                cur.execute("SELECT connection_id FROM connections WHERE connection_id = %s FOR UPDATE",
                            (connection_id,))
                cur.execute(
                    "UPDATE schema_versions SET status = 'archived', updated_at = NOW() "
                    "WHERE connection_id = %s AND status = 'published' AND version_id <> %s",
                    (connection_id, version_id),
                )
                cur.execute(
                    "UPDATE schema_versions SET status = 'published', updated_at = NOW() "
                    "WHERE version_id = %s AND connection_id = %s AND status = 'validated' RETURNING *",
                    (version_id, connection_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError("Version is not a validated version of this connection")
        return _row_to_version(dict(row))

    def get_published_version(self, connection_id: str) -> SemanticSchemaVersion | None:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM schema_versions
                    WHERE connection_id = %s AND status = 'published'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (connection_id,),
                )
                row = cur.fetchone()
        return _row_to_version(dict(row)) if row else None

    def list_versions(self, connection_id: str) -> list[SemanticSchemaVersion]:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM schema_versions WHERE connection_id = %s ORDER BY created_at DESC",
                    (connection_id,),
                )
                rows = cur.fetchall()
        return [_row_to_version(dict(r)) for r in rows]

    def archive_versions_for_connection(self, connection_id: str) -> None:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE schema_versions
                    SET status = 'archived', updated_at = NOW()
                    WHERE connection_id = %s AND status != 'archived'
                    """,
                    (connection_id,),
                )

    def get_published_connection_ids(self) -> set[str]:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT connection_id FROM schema_versions WHERE status = 'published'")
                rows = cur.fetchall()
        return {row["connection_id"] for row in rows}


# ── Row-to-model helpers ──────────────────────────────────────────────

def _row_to_connection(row: dict) -> ConnectionProfile:
    return ConnectionProfile(
        connection_id=row["connection_id"],
        display_name=row["display_name"],
        connector_type=row["connector_type"],
        status=row["status"],
        denied_columns=list(row.get("denied_columns") or []),
        owner_id=row.get("owner_id"),
        created_at=row["created_at"],
        engine=row.get("engine") or "legacy",
        workspace_id=row.get("workspace_id"),
        jammi_tables=_json_dict(row.get("jammi_tables")),
        mode=row.get("mode") or "live",
        copied_at=row.get("copied_at"),
        notes=list(row.get("notes") or []),
    )


def _jsonb(value):
    import psycopg2.extras

    return psycopg2.extras.Json(value)


def _json_dict(raw) -> dict:
    if not raw:
        return {}
    return dict(raw) if isinstance(raw, dict) else json.loads(raw)


def _row_to_version(row: dict) -> SemanticSchemaVersion:
    from app.models.connection import ValidationSummary, GenerationMetadata

    validation_summary = None
    if row.get("validation_summary"):
        raw = row["validation_summary"]
        data = raw if isinstance(raw, dict) else json.loads(raw)
        validation_summary = ValidationSummary(**data)

    generation_metadata = None
    if row.get("generation_metadata"):
        raw = row["generation_metadata"]
        data = raw if isinstance(raw, dict) else json.loads(raw)
        generation_metadata = GenerationMetadata(**data)

    return SemanticSchemaVersion(
        version_id=row["version_id"],
        connection_id=row["connection_id"],
        status=row["status"],
        schema_path=row.get("schema_path") or "",
        validation_summary=validation_summary,
        generation_metadata=generation_metadata,
        created_at=row["created_at"],
    )


# Backward-compat alias — default to JSONL (no config context at import time)
ConnectionStore = JsonlConnectionStore
