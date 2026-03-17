"""Authentication middleware for ALB + Cognito integration.

In production, the ALB authenticates users via Cognito and injects identity
headers. The backend trusts these headers (the ALB is the only ingress point).

In local development, set DEV_USER_ID to bypass authentication.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthUser:
    """Authenticated user extracted from ALB headers or dev bypass."""

    user_id: str
    email: str | None = None


def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency that returns the authenticated user.

    Resolution order:
    1. ALB-injected ``x-amzn-oidc-identity`` header (Cognito ``sub``).
    2. ``DEV_USER_ID`` environment variable (local development only).
    3. Raise 401.
    """
    # Production: ALB injects the Cognito sub as a plain-text header
    identity = request.headers.get("x-amzn-oidc-identity")
    if identity:
        # Optionally extract email from the x-amzn-oidc-data JWT payload.
        # The ALB signs this JWT; for now we just use the sub.
        email = request.headers.get("x-amzn-oidc-email")  # custom if set
        return AuthUser(user_id=identity.strip(), email=email)

    # Local development bypass
    config = getattr(request.app.state, "config", None)
    dev_user_id = getattr(config, "dev_user_id", None) if config else None
    if dev_user_id:
        return AuthUser(user_id=dev_user_id, email="dev@localhost")

    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
