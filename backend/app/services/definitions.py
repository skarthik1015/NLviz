"""Business definitions an owner confirmed for one connection (plan Phase 1, "confirmed definitions").

The semantic layer's metric conditions (`base_filter`: revenue without canceled orders) are inferred
by the schema generator. They are evidence about meaning, not authority: the SQL-primary path asks
before applying one that changes the answer (app.services.sql_checks, the definitions check).

A user's answer holds for their conversation. Only an explicit "use this for every question on this
connection" saves it here, so a conversation's choice never silently becomes a shared definition.
Each connection has its own file next to its published schemas, owned like the connection itself:
one connection's definitions are never read by another.

A record is keyed by the measured columns ("order_payments.payment_value") and remembers the
condition chosen (or none) and the definitions it was chosen among. It applies while that choice is
still one of the data model's options; after a republish that changes them, the question is asked
again.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from threading import Lock

logger = logging.getLogger(__name__)

FILE_NAME = "confirmed-definitions"


class DefinitionStore:
    def __init__(self, storage, connection_id: str):
        self._storage = storage
        self._connection_id = connection_id
        self._lock = Lock()
        self._records: dict[str, dict] = self._load()
        self._version = 0  # bumped by every change
        self._written = 0  # the newest version known to be on storage
        self._pending: dict[str, tuple[dict, int]] = {}  # confirmations whose write is in flight
        self._saved_version: dict[str, int] = {}

    def _load(self) -> dict[str, dict]:
        if self._storage is None:
            return {}
        try:
            text = self._storage.read_text(self._connection_id, FILE_NAME)
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 - unreadable: ask again rather than guess
            logger.warning("Confirmed definitions unreadable for %s: %s", self._connection_id, exc)
            return {}
        try:
            records = json.loads(text) if text else {}
        except json.JSONDecodeError:
            logger.warning("Confirmed definitions malformed for %s", self._connection_id)
            return {}
        return records if isinstance(records, dict) else {}

    def all(self) -> dict[str, dict]:
        """Confirmations that are saved. One whose write failed is not here: it holds only in the
        conversation that made it."""
        with self._lock:
            return {key: dict(value) for key, value in self._records.items()}

    def confirm(self, key: str, condition: str | None, label: str, options: list[str | None]) -> None:
        """Save a confirmation; raises when it could not be written (then nothing changes).

        Writes run outside the lock, each with the saved records plus every confirmation still in
        flight. A confirmation becomes visible (all()) once a write containing it succeeded. A write
        that finishes after a newer one rewrites the newest snapshot, so an older write never wins."""
        record = {"filter": condition, "label": label, "options": options,
                  "confirmed_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self._version += 1
            mine = self._version
            self._pending[key] = (record, mine)
        if self._storage is None:
            with self._lock:
                self._commit({key: mine})
            return
        while True:
            with self._lock:
                version = self._version
                included = {k: v for k, (_, v) in self._pending.items()}
                snapshot = {**self._records, **{k: r for k, (r, _) in self._pending.items()}}
            try:
                self._storage.write_text(self._connection_id, FILE_NAME,
                                         json.dumps(snapshot, indent=2, sort_keys=True))
            except Exception:
                with self._lock:
                    if self._saved_version.get(key) == mine:
                        return  # a newer write already saved this confirmation
                    if self._pending.get(key, (None, 0))[1] == mine:
                        del self._pending[key]
                raise
            with self._lock:
                self._commit(included)
                if version >= self._written:
                    self._written = version
                    return
                # A newer snapshot was written while ours was in flight, and ours overwrote it.

    def _commit(self, included: dict[str, int]) -> None:
        for key, version in included.items():
            entry = self._pending.get(key)
            if entry is not None and entry[1] == version:
                self._records[key] = self._pending.pop(key)[0]
                self._saved_version[key] = version
