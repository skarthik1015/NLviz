from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_feedback_store
from app.models import FeedbackRequest, FeedbackResponse
from app.security.auth import AuthUser, get_current_user
from app.services.feedback_store import BaseFeedbackStore, FeedbackConflict

router = APIRouter(tags=["feedback"])


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    request: FeedbackRequest,
    store: BaseFeedbackStore = Depends(get_feedback_store),
    user: AuthUser = Depends(get_current_user),
) -> FeedbackResponse:
    try:
        status, feedback = store.upsert(
            owner_id=user.user_id,
            query_id=request.query_id,
            rating=request.rating,
            comment=request.comment,
            idempotency_key=request.idempotency_key,
        )
    except FeedbackConflict:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_REUSED") from None
    return FeedbackResponse(status=status, feedback=feedback)
