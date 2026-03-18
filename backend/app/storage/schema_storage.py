"""Schema file storage abstraction.

Provides a unified interface for saving and loading auto-generated semantic
YAML files regardless of whether they live on local disk or in S3.

Local mode: used in development and tests — no AWS credentials needed.
S3 mode:    used in production (ECS) — files survive container restarts.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage.s3_storage import S3Storage
    from app.config import AppConfig

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_DIR = Path(__file__).resolve().parents[2] / "data" / "schemas"


class BaseSchemaStorage(ABC):
    """Abstract storage backend for generated semantic YAML files."""

    @abstractmethod
    def save(self, connection_id: str, version_id: str, content: str) -> str:
        """Persist a schema YAML string.

        Returns:
            A path string that can be passed back to ``load()`` later.
            For local storage this is a filesystem path; for S3 it is an
            ``s3://bucket/key`` URL.
        """

    @abstractmethod
    def load(self, path: str) -> str:
        """Load and return the YAML string from the given path.

        Args:
            path: Value previously returned by ``save()``.
        """

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete a previously saved schema artifact."""

    # ── Factory ───────────────────────────────────────────────────────

    @classmethod
    def create(cls, config: "AppConfig", s3: "S3Storage | None" = None) -> "BaseSchemaStorage":
        """Return the appropriate implementation based on AppConfig."""
        if config.schema_bucket and s3 is not None:
            logger.info("Schema storage: S3 bucket %s", config.schema_bucket)
            return S3SchemaStorage(s3=s3)
        logger.info("Schema storage: local filesystem at %s", _DEFAULT_LOCAL_DIR)
        return LocalSchemaStorage()


class LocalSchemaStorage(BaseSchemaStorage):
    """Stores schema YAML files on the local filesystem.

    Safe for development and single-container deployments. Files are stored at
    ``backend/data/schemas/{connection_id}/{version_id}.yaml``.
    """

    def __init__(self, base_dir: Path = _DEFAULT_LOCAL_DIR) -> None:
        self._base_dir = base_dir

    def save(self, connection_id: str, version_id: str, content: str) -> str:
        path = self._base_dir / connection_id / f"{version_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.debug("Schema saved locally: %s", path)
        return str(path)

    def load(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def delete(self, path: str) -> None:
        file_path = Path(path)
        if file_path.exists():
            file_path.unlink()


class S3SchemaStorage(BaseSchemaStorage):
    """Stores schema YAML files in S3.

    Key pattern: ``schemas/{connection_id}/{version_id}.yaml``

    Files survive container restarts and are shared across all ECS task
    instances because they are read from S3 on each request.
    """

    def __init__(self, s3: "S3Storage") -> None:
        self._s3 = s3

    def save(self, connection_id: str, version_id: str, content: str) -> str:
        key = f"schemas/{connection_id}/{version_id}.yaml"
        url = self._s3.upload_bytes(key, content.encode("utf-8"))
        logger.debug("Schema saved to S3: %s", url)
        return url

    def load(self, path: str) -> str:
        if path.startswith("s3://"):
            key = self._s3.key_from_url(path)
        else:
            # Graceful fallback: treat as a local path (migration period)
            logger.warning("S3SchemaStorage.load() received a local path: %s — falling back to filesystem", path)
            return Path(path).read_text(encoding="utf-8")
        data = self._s3.download_bytes(key)
        return data.decode("utf-8")

    def delete(self, path: str) -> None:
        if path.startswith("s3://"):
            self._s3.delete(self._s3.key_from_url(path))
            return
        logger.warning("S3SchemaStorage.delete() received a local path: %s â€” falling back to filesystem", path)
        local_path = Path(path)
        if local_path.exists():
            local_path.unlink()
