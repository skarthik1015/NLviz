"""Simple JSONL-based audit event logger."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_PATH = Path(__file__).resolve().parents[2] / "data" / "audit.jsonl"


class AuditLog:
    def __init__(self, path: Path = _AUDIT_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event_type: str,
        connection_id: str | None = None,
        owner_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "connection_id": connection_id,
            "owner_id": owner_id,
            "metadata": metadata or {},
        }
        try:
            with self._path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            logger.warning("Failed to write audit event: %s", event_type, exc_info=True)

        # Also emit as structured log
        logger.info(
            "audit: %s connection=%s owner=%s",
            event_type,
            connection_id,
            owner_id,
            extra=entry,
        )
