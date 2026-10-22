"""Pass-through ASGI question logging, including streaming result/error events.

Bodies are inspected with bounded buffers and always forwarded byte-for-byte.
Only allowlisted response fields enter logs; SQL, rows and explanations never do.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from uuid import uuid4

from fastapi import Depends
from app.security.auth import AuthUser, get_current_user
from .pricing import is_local
from .service import QuestionContext, question_context

logger = logging.getLogger("nlviz.questions")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_LIMIT = 1024 * 1024


def safe_id(value):
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def capture_authenticated_user(user: AuthUser = Depends(get_current_user)) -> None:
    """Use the existing cached auth dependency; never trust raw identity headers."""
    context = question_context.get()
    if context is not None:
        context.user = hashlib.sha256(user.user_id.encode()).hexdigest()


class ResponseObserver:
    def __init__(self):
        self.buffer = bytearray()
        self.sse = False
        self.overflow = False
        self.result = {}

    def observe_json(self, data):
        if not isinstance(data, dict):
            return
        query_id = safe_id(data.get("query_id"))
        if query_id:
            self.result["query_id"] = query_id
        status = data.get("status")
        if status in ("answered", "clarification", "unsupported"):
            self.result["status"] = status
        if isinstance(status, int) and 400 <= status <= 599:
            self.result["status"] = "error"
        audit = data.get("audit")
        if isinstance(audit, dict) and isinstance(audit.get("llm_calls"), list):
            self.result["audit_calls"] = audit["llm_calls"]

    def feed(self, body: bytes, *, final=False):
        if self.overflow:
            return
        if len(self.buffer) + len(body) > _LIMIT:
            self.overflow = True
            self.buffer.clear()
            return
        self.buffer.extend(body)
        if self.sse:
            while b"\n\n" in self.buffer:
                event, rest = bytes(self.buffer).split(b"\n\n", 1)
                self.buffer = bytearray(rest)
                lines = event.split(b"\n")
                kind = next((line[6:].strip() for line in lines if line.startswith(b"event:")), b"")
                if kind not in {b"result", b"error"}:
                    continue
                payload = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
                self.parse(payload)
        elif final:
            self.parse(self.buffer)
            self.buffer.clear()

    def parse(self, payload):
        try:
            self.observe_json(json.loads(payload))
        except (ValueError, UnicodeDecodeError, RecursionError):
            pass


def usage_summary(calls, prices):
    if not calls:
        return {"model": None, "prompt_tokens": None, "completion_tokens": None,
                "cost_usd": None, "usage_complete": False}
    prompt = completion = 0
    cost = 0
    complete = True
    models = set()
    for call in calls:
        if not isinstance(call, dict):
            complete = False
            continue
        model, provider = call.get("model"), call.get("provider")
        if safe_id(model):
            models.add(model)
        p, c = call.get("prompt_tokens"), call.get("completion_tokens")
        if type(p) is not int or type(c) is not int or p < 0 or c < 0:
            complete = False
            continue
        prompt += p
        completion += c
        if not isinstance(provider, str) or not isinstance(model, str):
            complete = False
            continue
        # Per-call hooks know endpoint locality. Audits lack base_url: never assume free.
        if call.get("cost_usd") is not None:
            from .pricing import money
            try:
                cost += money(call["cost_usd"])
            except ValueError:
                complete = False
        elif is_local(provider or ""):
            continue
        else:
            price = prices.get(provider, model)
            if price is None:
                complete = False
            else:
                cost += price.cost(p, c)
    return {"model": sorted(models), "prompt_tokens": prompt, "completion_tokens": completion,
            "cost_usd": str(cost) if complete else None, "usage_complete": complete}


class QuestionLoggingMiddleware:
    def __init__(self, app, *, api_prefix="/api"):
        self.app = app
        self.paths = {f"{api_prefix}/chat{suffix}" for suffix in ("", "/resume", "/stream", "/resume/stream")}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope["path"] not in self.paths:
            return await self.app(scope, receive, send)
        service = getattr(scope["app"].state, "observability", None)
        if service is None:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        request_id = safe_id(headers.get(b"x-request-id", b"").decode("latin1")) or str(uuid4())
        context = QuestionContext(request_id)
        token = question_context.set(context)
        observer = ResponseObserver()
        request_body = bytearray()
        request_overflow = False
        status = 500
        failed = False
        started = time.perf_counter()

        async def observed_receive():
            nonlocal request_overflow
            message = await receive()
            if service.debug_text and message["type"] == "http.request":
                body = message.get("body", b"")
                if len(request_body) + len(body) <= 65536 and not request_overflow:
                    request_body.extend(body)
                else:
                    request_overflow = True
                    request_body.clear()
            return message

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                content_type = dict(message.get("headers", [])).get(b"content-type", b"")
                observer.sse = content_type.startswith(b"text/event-stream")
                message = {**message, "headers": [
                    (key, value) for key, value in message.get("headers", []) if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode("ascii"))]}
            if message["type"] == "http.response.body":
                observer.feed(message.get("body", b""), final=not message.get("more_body", False))
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException:
            failed = True
            raise
        finally:
            question_context.reset(token)
            # Prefer authoritative per-attempt hooks; audit fallback is observational only.
            calls = context.calls or observer.result.get("audit_calls", [])
            result = {"event": "question", "entry_point": scope["path"], "request_id": request_id,
                      "query_id": observer.result.get("query_id"), "user": context.user,
                      "connection": safe_id(headers.get(b"x-connection-id", b"default").decode("latin1")),
                      "status": "error" if failed else observer.result.get(
                          "status", "error" if status >= 400 else "unknown"),
                      "http_status": status, "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                      "response_observation_complete": not observer.overflow,
                      **usage_summary(calls, service.prices)}
            if service.debug_text and request_body:
                try:
                    body = json.loads(request_body)
                    text = body.get("question", body.get("answer"))
                    if isinstance(text, str):
                        result["question"] = text[:4096]
                except (ValueError, UnicodeDecodeError, AttributeError, RecursionError):
                    pass
            logger.info(json.dumps(result, separators=(",", ":")))
