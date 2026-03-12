"""Per-connection credential storage.

Two concrete implementations share a common ABC:

- ``LocalSecretStore``:             Fernet-encrypted files on local disk.
                                    Used in development (SECRET_BACKEND=local).
- ``AWSSecretsManagerStore``:       AWS Secrets Manager.
                                    Used in production (SECRET_BACKEND=aws_secrets_manager).

Factory::

    store = BaseSecretStore.create(config)
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import AppConfig

from app.models.connection import SecretRef

logger = logging.getLogger(__name__)

_SECRETS_DIR = Path(__file__).resolve().parents[2] / "data" / ".secrets"


# ── Abstract Base ─────────────────────────────────────────────────────

class BaseSecretStore(ABC):
    """Storage contract for per-connection credentials."""

    @abstractmethod
    def put(self, connection_id: str, params: dict) -> SecretRef:
        """Persist credentials for a connection.

        Returns a ``SecretRef`` that can be passed to ``get()`` later.
        """

    @abstractmethod
    def get(self, secret_ref: SecretRef) -> dict:
        """Retrieve credentials by their ``SecretRef``."""

    @abstractmethod
    def get_by_connection_id(self, connection_id: str) -> dict:
        """Retrieve credentials directly by connection ID."""

    @abstractmethod
    def delete(self, connection_id: str) -> None:
        """Delete credentials for a connection."""

    # ── Factory ───────────────────────────────────────────────────────

    @classmethod
    def create(cls, config: "AppConfig", sm_client=None) -> "BaseSecretStore":
        """Return the appropriate implementation based on ``AppConfig``."""
        if config.secret_backend == "aws_secrets_manager":
            logger.info("SecretStore: AWS Secrets Manager (prefix=%s)", config.secret_prefix)
            return AWSSecretsManagerStore(
                prefix=config.secret_prefix,
                region=config.aws_region,
                client=sm_client,
            )
        logger.info("SecretStore: local Fernet-encrypted files")
        return LocalSecretStore()


# ── Backward-compat alias ─────────────────────────────────────────────
SecretStore = None  # set at bottom


# ── Local Implementation ──────────────────────────────────────────────

def _get_fernet():
    """Lazy import + key load so the module doesn't crash if cryptography is absent."""
    from cryptography.fernet import Fernet

    key = os.getenv("SECRET_STORE_KEY")
    if not key:
        logger.warning(
            "SECRET_STORE_KEY not set; generating ephemeral key. "
            "Secrets will be unreadable after restart. Set SECRET_STORE_KEY for persistence."
        )
        key = Fernet.generate_key().decode()
        os.environ["SECRET_STORE_KEY"] = key
    return Fernet(key.encode() if isinstance(key, str) else key)


class LocalSecretStore(BaseSecretStore):
    """Fernet-encrypted JSON files — one file per connection.

    Acceptable for local development. In production, swap for
    ``AWSSecretsManagerStore``.
    """

    def __init__(self, secrets_dir: Path = _SECRETS_DIR) -> None:
        self._dir = secrets_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def put(self, connection_id: str, params: dict) -> SecretRef:
        store_key = f"{connection_id}.enc"
        path = self._dir / store_key
        fernet = _get_fernet()
        path.write_bytes(fernet.encrypt(json.dumps(params).encode("utf-8")))
        return SecretRef(connection_id=connection_id, store_key=store_key)

    def get(self, secret_ref: SecretRef) -> dict:
        path = self._dir / secret_ref.store_key
        if not path.exists():
            raise FileNotFoundError(f"Secret not found for connection {secret_ref.connection_id}")
        fernet = _get_fernet()
        return json.loads(fernet.decrypt(path.read_bytes()).decode("utf-8"))

    def get_by_connection_id(self, connection_id: str) -> dict:
        return self.get(SecretRef(connection_id=connection_id, store_key=f"{connection_id}.enc"))

    def delete(self, connection_id: str) -> None:
        path = self._dir / f"{connection_id}.enc"
        if path.exists():
            path.unlink()


# ── AWS Secrets Manager Implementation ────────────────────────────────

class AWSSecretsManagerStore(BaseSecretStore):
    """Stores per-connection credentials in AWS Secrets Manager.

    Secret naming convention::

        {prefix}/connections/{connection_id}

    e.g. ``nl-query-tool/connections/123e4567-e89b-12d3-a456-426614174000``

    The value stored is a JSON object matching the ``params`` dict passed to
    ``put()``.  For DuckDB uploads this is ``{"db_path": "s3://..."}``; for
    Postgres it is the full connection params dict.
    """

    def __init__(self, prefix: str, region: str, client=None) -> None:
        self._prefix = prefix
        self._region = region
        self._client = client  # injectable for unit tests

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy import
            self._client = boto3.client("secretsmanager", region_name=self._region)
        return self._client

    def _secret_name(self, connection_id: str) -> str:
        return f"{self._prefix}/connections/{connection_id}"

    def put(self, connection_id: str, params: dict) -> SecretRef:
        name = self._secret_name(connection_id)
        value = json.dumps(params)
        client = self._get_client()
        try:
            client.create_secret(Name=name, SecretString=value)
            logger.debug("Created secret %s", name)
        except client.exceptions.ResourceExistsException:
            client.put_secret_value(SecretId=name, SecretString=value)
            logger.debug("Updated secret %s", name)
        return SecretRef(connection_id=connection_id, store_key=name)

    def get(self, secret_ref: SecretRef) -> dict:
        return self.get_by_connection_id(secret_ref.connection_id)

    def get_by_connection_id(self, connection_id: str) -> dict:
        name = self._secret_name(connection_id)
        response = self._get_client().get_secret_value(SecretId=name)
        return json.loads(response["SecretString"])

    def delete(self, connection_id: str) -> None:
        name = self._secret_name(connection_id)
        client = self._get_client()
        try:
            client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            logger.debug("Deleted secret %s", name)
        except client.exceptions.ResourceNotFoundException:
            logger.debug("Secret %s not found; nothing to delete", name)


# Backward-compat alias
SecretStore = LocalSecretStore
