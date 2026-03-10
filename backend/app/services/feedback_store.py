from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.models import FeedbackRecord

_JSONL_PATH = Path(__file__).resolve().parents[2] / "data" / "feedback.jsonl"


@dataclass
class _FeedbackInternal:
    record: FeedbackRecord


class FeedbackStore:
    def __init__(self):
        self._records_by_idempotency_key: dict[str, _FeedbackInternal] = {}
        self._records: dict[str, _FeedbackInternal] = {}
        self._lock = Lock()
        self._jsonl_path = _JSONL_PATH
        self._load_from_jsonl()

    def _load_from_jsonl(self) -> None:
        if not self._jsonl_path.exists():
            return
        try:
            with self._jsonl_path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    record = FeedbackRecord.model_validate(json.loads(line))
                    internal = _FeedbackInternal(record=record)
                    self._records[record.feedback_id] = internal
                    if record.idempotency_key:
                        self._records_by_idempotency_key[record.idempotency_key] = internal
        except Exception:
            pass  # corrupt JSONL doesn't crash startup; data already in memory is used

    def _append_to_jsonl(self, record: FeedbackRecord) -> None:
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self._jsonl_path.open("a", encoding="utf-8") as fp:
                fp.write(record.model_dump_json() + "\n")
        except Exception:
            pass  # persistence failure is non-fatal

    def upsert(
        self,
        *,
        query_id: str,
        rating: str,
        comment: str | None,
        idempotency_key: str | None,
    ) -> tuple[str, FeedbackRecord]:
        with self._lock:
            if idempotency_key and idempotency_key in self._records_by_idempotency_key:
                existing = self._records_by_idempotency_key[idempotency_key].record
                updated = existing.model_copy(update={"rating": rating, "comment": comment})
                internal = _FeedbackInternal(record=updated)
                self._records[updated.feedback_id] = internal
                self._records_by_idempotency_key[idempotency_key] = internal
                self._append_to_jsonl(updated)
                return "updated", updated

            feedback = FeedbackRecord(
                feedback_id=str(uuid4()),
                query_id=query_id,
                rating=rating,
                comment=comment,
                idempotency_key=idempotency_key,
                created_at=datetime.now(timezone.utc),
            )
            internal = _FeedbackInternal(record=feedback)
            self._records[feedback.feedback_id] = internal
            if idempotency_key:
                self._records_by_idempotency_key[idempotency_key] = internal
            self._append_to_jsonl(feedback)
            return "created", feedback
