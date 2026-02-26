from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_query_service
from app.models import ChatRequest, ChatResponse
from app.security import SQLSafetyError
from app.services import QueryService

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, service: QueryService = Depends(get_query_service)) -> ChatResponse:
    try:
        return service.run_question(request.question, explicit_intent=request.intent)
    except (ValueError, KeyError, SQLSafetyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Chat pipeline failed: {exc}") from exc
