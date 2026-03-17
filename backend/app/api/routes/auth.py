"""Authentication routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.security.auth import AuthUser, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def get_me(user: AuthUser = Depends(get_current_user)) -> dict:
    return {"user_id": user.user_id, "email": user.email}
