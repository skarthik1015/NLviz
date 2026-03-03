from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


@dataclass
class _TokenBucket:
    tokens: float
    updated_at: float


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        requests_per_minute: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(app)
        self._requests_per_minute = max(0, requests_per_minute)
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = Lock()

    async def dispatch(self, request: Request, call_next):
        if self._requests_per_minute <= 0 or request.url.path != "/chat":
            return await call_next(request)

        identifier = self._client_identifier(request)
        allowed, retry_after = self._consume(identifier)
        if not allowed:
            retry_seconds = max(1, math.ceil(retry_after))
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded. Retry in {retry_seconds} seconds."},
                headers={"Retry-After": str(retry_seconds)},
            )

        return await call_next(request)

    def _client_identifier(self, request: Request) -> str:
        forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded_for:
            return forwarded_for
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        client = request.client
        if client and client.host:
            return client.host
        return "anonymous"

    def _consume(self, identifier: str) -> tuple[bool, float]:
        now = self._clock()
        capacity = float(self._requests_per_minute)
        refill_rate = capacity / 60.0

        with self._lock:
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

            retry_after = 0.0 if refill_rate == 0 else (1.0 - bucket.tokens) / refill_rate
            return False, retry_after
