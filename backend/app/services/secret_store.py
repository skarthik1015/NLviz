from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.models.connection import SecretRef

logger = logging.getLogger(__name__)

_SECRETS_DIR = Path(__file__).resolve().parents[2] / "data" / ".secrets"


def _get_fernet():
    """Lazy import + key load so the module doesn't crash if cryptography is missing."""
    from cryptography.fernet import Fernet

    key = os.getenv("SECRET_STORE_KEY")
    if not key:
        # Auto-generate and warn — acceptable for local dev, not production
        logger.warning(
            "SECRET_STORE_KEY not set; generating ephemeral key. "
            "Secrets will be unreadable after restart. Set SECRET_STORE_KEY for persistence."
        )
        key = Fernet.generate_key().decode()
        os.environ["SECRET_STORE_KEY"] = key
    return Fernet(key.encode() if isinstance(key, str) else key)


class SecretStore:
    """Fernet-encrypted file-based secret storage.

    Each connection's credentials are stored in a separate encrypted JSON file.
    Swap this implementation for AWS Secrets Manager / Vault in production.
    """

    def __init__(self, secrets_dir: Path = _SECRETS_DIR):
        self._dir = secrets_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def put(self, connection_id: str, params: dict) -> SecretRef:
        store_key = f"{connection_id}.enc"
        path = self._dir / store_key
        fernet = _get_fernet()
        plaintext = json.dumps(params).encode("utf-8")
        path.write_bytes(fernet.encrypt(plaintext))
        return SecretRef(connection_id=connection_id, store_key=store_key)

    def get(self, secret_ref: SecretRef) -> dict:
        path = self._dir / secret_ref.store_key
        if not path.exists():
            raise FileNotFoundError(f"Secret not found for connection {secret_ref.connection_id}")
        fernet = _get_fernet()
        plaintext = fernet.decrypt(path.read_bytes())
        return json.loads(plaintext.decode("utf-8"))

    def get_by_connection_id(self, connection_id: str) -> dict:
        return self.get(SecretRef(connection_id=connection_id, store_key=f"{connection_id}.enc"))

    def delete(self, connection_id: str) -> None:
        path = self._dir / f"{connection_id}.enc"
        if path.exists():
            path.unlink()
