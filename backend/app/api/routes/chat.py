import json
import logging
import re
from collections.abc import Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.agent.graph import NoPendingClarificationError
from app.connectors.jammi_connector import CrossSourceQueryError
from app.dependencies import get_conversation_scope, get_query_service
from app.llm import is_budget_exceeded
from app.models import ChatRequest, ChatResponse, ChatResumeRequest
from app.rate_limit import QueryLimiter
from app.security import SQLSafetyError
from app.security.auth import AuthUser, get_current_user
from app.services.intent_mapper import IntentMappingError
from app.services.query_service import QueryService

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


def _error(exc: Exception) -> tuple[int, str]:
    """The status and user-facing detail for an error; internals never reach the client."""
    if isinstance(exc, NoPendingClarificationError):
        return 409, "NO_PENDING_CLARIFICATION"
    if is_budget_exceeded(exc):
        # The monthly hosted-model budget (LLM_MONTHLY_BUDGET_USD) is spent: no call was made.
        logger.warning("Question refused: LLM budget exceeded")
        return 503, "LLM_BUDGET_EXCEEDED"
    if isinstance(exc, CrossSourceQueryError):
        # A known engine limitation (Jammi issue J28): tell the user what to ask instead.
        return 400, str(exc)
    if isinstance(exc, IntentMappingError):
        # Neither the LLM (after repairs) nor the fallback found a reading ("Only 2023" with no
        # usable previous turn): ask the user to rephrase rather than report an unsafe query.
        logger.info("Question not understood: %s", str(exc)[:300])
        return 422, "QUESTION_NOT_UNDERSTOOD"
    if isinstance(exc, (ValueError, KeyError, SQLSafetyError)):
        # The client sees a generic code; the log keeps the reason (a compiler bug looked like this).
        logger.warning("Question refused as invalid or unsafe: %s: %s", type(exc).__name__, str(exc)[:500])
        return 400, "INVALID_OR_UNSAFE_QUERY"
    if _DATABASE_DOWN.search(str(exc)):
        # A live source whose database is down: Jammi answers after its 30 s pool timeout with
        # "PostgreSQL connection failed ... Timed out in bb8" (J37). Not our failure, and retryable.
        logger.warning("Question failed: the database is unavailable: %s", str(exc)[:300])
        return 503, "DATABASE_UNAVAILABLE"
    logger.error("Question failed", exc_info=exc)
    return 500, "An internal error occurred. Please try again."


_DATABASE_DOWN = re.compile(r"(PostgreSQL|MySQL) connection failed|Timed out in bb8|Connection refused|"
                            r"could not connect to server|Can't connect to MySQL server", re.IGNORECASE)


def get_query_limiter(request: Request) -> QueryLimiter | None:
    return getattr(request.app.state, "query_limiter", None)


def _admit(limiter: QueryLimiter | None, user: AuthUser) -> Callable[[], None]:
    return limiter.admit(user.user_id) if limiter is not None else (lambda: None)


async def _answer(run: Callable[[], ChatResponse], release: Callable[[], None]) -> ChatResponse:
    """Runs the question off the event loop (the graph, the LLM and the engine all block), so one
    slow question does not stall every other request."""
    try:
        return await run_in_threadpool(run)
    except Exception as exc:
        status, detail = _error(exc)
        raise HTTPException(status_code=status, detail=detail) from exc
    finally:
        release()


def _sse(events: Callable[[], Iterator[dict]], release: Callable[[], None]) -> StreamingResponse:
    """Server-sent events: "step" per graph node, then "result" or "error". The question's slot is
    released when the stream ends, however it ends."""

    def body() -> Iterator[str]:
        try:
            for event in events():
                kind = event.pop("event")
                if kind == "result":
                    data = event["response"].model_dump_json()
                else:
                    data = json.dumps(event, default=str)
                yield f"event: {kind}\ndata: {data}\n\n"
        except Exception as exc:
            status, detail = _error(exc)
            yield f"event: error\ndata: {json.dumps({'status': status, 'detail': detail})}\n\n"
        finally:
            release()

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    service: QueryService = Depends(get_query_service),
    scope: str = Depends(get_conversation_scope),
    user: AuthUser = Depends(get_current_user),
    limiter: QueryLimiter | None = Depends(get_query_limiter),
) -> ChatResponse:
    release = _admit(limiter, user)
    return await _answer(lambda: service.run_question(
        request.question, explicit_intent=request.intent, debug=request.debug,
        conversation_id=request.conversation_id, scope=scope,
    ), release)


@router.post("/chat/resume", response_model=ChatResponse)
async def resume_chat(
    request: ChatResumeRequest,
    service: QueryService = Depends(get_query_service),
    scope: str = Depends(get_conversation_scope),
    user: AuthUser = Depends(get_current_user),
    limiter: QueryLimiter | None = Depends(get_query_limiter),
) -> ChatResponse:
    """Answer the clarification a conversation is paused on."""
    release = _admit(limiter, user)
    return await _answer(lambda: service.resume(request.conversation_id, request.answer, debug=request.debug,
                                                scope=scope, remember=request.remember), release)


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    service: QueryService = Depends(get_query_service),
    scope: str = Depends(get_conversation_scope),
    user: AuthUser = Depends(get_current_user),
    limiter: QueryLimiter | None = Depends(get_query_limiter),
) -> StreamingResponse:
    """`/chat` as server-sent events, so the UI can show each step as it happens."""
    release = _admit(limiter, user)
    return _sse(lambda: service.stream_question(
        request.question, explicit_intent=request.intent, debug=request.debug,
        conversation_id=request.conversation_id, scope=scope,
    ), release)


@router.post("/chat/resume/stream")
async def resume_chat_stream(
    request: ChatResumeRequest,
    service: QueryService = Depends(get_query_service),
    scope: str = Depends(get_conversation_scope),
    user: AuthUser = Depends(get_current_user),
    limiter: QueryLimiter | None = Depends(get_query_limiter),
) -> StreamingResponse:
    release = _admit(limiter, user)
    return _sse(lambda: service.stream_resume(request.conversation_id, request.answer, debug=request.debug,
                                              scope=scope, remember=request.remember), release)
