"""Per-user limits on question work: a request rate and a number of questions running at once.

Applied by the question routes themselves (/chat, /chat/stream, /chat/resume, /chat/resume/stream)
after the user is known, so the limit follows the user, not a client-supplied address header, and
does not depend on the URL prefix. The old middleware matched the path "/chat" exactly: the UI's
streaming routes, the resume routes and any API_PREFIX bypassed it (review B2).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from fastapi import HTTPException


@dataclass
class _TokenBucket:
    tokens: float
    updated_at: float


class QueryLimiter:
    def __init__(self, *, requests_per_minute: int, max_concurrent: int = 2,
                 clock: Callable[[], float] | None = None) -> None:
        self._requests_per_minute = max(0, requests_per_minute)
        self._max_concurrent = max(0, max_concurrent)
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _TokenBucket] = {}
        self._running: dict[str, int] = {}
        self._lock = Lock()

    def admit(self, user_id: str) -> Callable[[], None]:
        """Admit one question for the user, or raise 429. Returns the release function, which must
        be called once the question's work is done (also for a stream: when it ends)."""
        with self._lock:
            if self._max_concurrent and self._running.get(user_id, 0) >= self._max_concurrent:
                raise HTTPException(status_code=429, detail="Too many questions running. Wait for one to finish.",
                                    headers={"Retry-After": "5"})
            if self._requests_per_minute:
                allowed, retry_after = self._consume(user_id)
                if not allowed:
                    seconds = max(1, math.ceil(retry_after))
                    raise HTTPException(status_code=429, detail=f"Rate limit exceeded. Retry in {seconds} seconds.",
                                        headers={"Retry-After": str(seconds)})
            self._running[user_id] = self._running.get(user_id, 0) + 1

        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                left = self._running.get(user_id, 1) - 1
                if left > 0:
                    self._running[user_id] = left
                else:
                    self._running.pop(user_id, None)

        return release

    def _consume(self, identifier: str) -> tuple[bool, float]:
        """Token bucket; the caller holds the lock."""
        now = self._clock()
        capacity = float(self._requests_per_minute)
        refill_rate = capacity / 60.0
        bucket = self._buckets.get(identifier)
        if bucket is None:
            bucket = _TokenBucket(tokens=capacity, updated_at=now)
            self._buckets[identifier] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_rate)
            bucket.updated_at = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        return False, (1.0 - bucket.tokens) / refill_rate
