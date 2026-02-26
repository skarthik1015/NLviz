from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from app.models import FeedbackRecord


@dataclass
class _FeedbackInternal:
    record: FeedbackRecord


class FeedbackStore:
    def __init__(self):
        self._records_by_idempotency_key: dict[str, _FeedbackInternal] = {}
        self._records: dict[str, _FeedbackInternal] = {}
        self._lock = Lock()

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
            return "created", feedback
