from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

from app.models.connection import (
    ConnectionProfile,
    SemanticSchemaVersion,
)

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_CONNECTIONS_PATH = _DATA_DIR / "connections.jsonl"
_SCHEMA_VERSIONS_PATH = _DATA_DIR / "schema_versions.jsonl"


class ConnectionStore:
    def __init__(
        self,
        connections_path: Path = _CONNECTIONS_PATH,
        versions_path: Path = _SCHEMA_VERSIONS_PATH,
    ):
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
