from fastapi import APIRouter, Depends

from app.dependencies import get_feedback_store
from app.models import FeedbackRequest, FeedbackResponse
from app.services.feedback_store import FeedbackStore

router = APIRouter(tags=["feedback"])


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    request: FeedbackRequest,
    store: FeedbackStore = Depends(get_feedback_store),
) -> FeedbackResponse:
    status, feedback = store.upsert(
        query_id=request.query_id,
        rating=request.rating,
        comment=request.comment,
        idempotency_key=request.idempotency_key,
    )
    return FeedbackResponse(status=status, feedback=feedback)
