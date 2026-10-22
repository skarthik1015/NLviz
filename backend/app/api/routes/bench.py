"""/bench/sql: the text-to-SQL baseline (app.services.text_to_sql). Registered only when
BENCH_ENDPOINTS=on (app.api.build_api_router), and never in production."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.dependencies import _get_runtime
from app.llm import is_budget_exceeded
from app.security.auth import AuthUser, get_current_user
from app.services.intent_mapper import IntentMapperConfig
from app.services.replay import replay_case
from app.services.text_to_sql import answer

router = APIRouter(prefix="/bench", tags=["bench"])


class BenchQuestion(BaseModel):
    question: str = Field(..., min_length=1)
    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    debug: bool = True
    # The pushdown guard the app's SQL path uses (app.services.generated_sql); off: the ablation baseline.
    guard: bool = False


@router.post("/sql")
async def text_to_sql(body: BenchQuestion, request: Request,
                      user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    runtime = _get_runtime(request, user)
    conversation = f"{user.user_id}/{body.conversation_id}" if body.conversation_id else None
    try:
        result = await run_in_threadpool(answer, body.question, conversation, runtime, IntentMapperConfig.from_env(),
                                         body.guard)
    except Exception as exc:  # the baseline's failures are measured, not hidden
        if is_budget_exceeded(exc):  # as /chat answers it: the cap, not a baseline failure
            raise HTTPException(status_code=503, detail="LLM_BUDGET_EXCEEDED") from exc
        raise HTTPException(status_code=500, detail=f"BENCH_FAILED: {type(exc).__name__}: {str(exc)[:300]}") from exc
    result["conversation_id"] = body.conversation_id
    return result


class ReplayTurn(BaseModel):
    question: str
    proposals: list[dict[str, Any]] = Field(default_factory=list)  # audit "proposal" steps, in order
    answers: list[Any] = Field(default_factory=list)  # replies to the clarifications, in order


class ReplayCase(BaseModel):
    turns: list[ReplayTurn]
    phrase_rewrites: bool


@router.post("/replay")
async def replay(body: ReplayCase, request: Request, user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    """Recorded LLM proposals through the agent again, with the phrase rules on or off (step 0d)."""
    runtime = _get_runtime(request, user)
    turns = [turn.model_dump() for turn in body.turns]
    try:
        results = await run_in_threadpool(replay_case, runtime, turns, body.phrase_rewrites)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"REPLAY_FAILED: {type(exc).__name__}: {str(exc)[:300]}") from exc
    return {"turns": results}
