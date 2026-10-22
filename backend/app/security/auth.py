"""Authentication for ALB + Cognito, and the local development identity.

Behind an AWS ALB with OIDC (TRUST_PROXY_IDENTITY=true), the ALB signs the user's claims into the
`x-amzn-oidc-data` header (ES256). Only that signed token is trusted: its signature is checked with
the ALB's public key for the token's key id, its `signer` must be the expected ALB (ALB_ARN), and it
must not have expired. The plain `x-amzn-oidc-identity` header proves nothing on its own: any caller
that reaches the backend directly can set it (review B1).

Without TRUST_PROXY_IDENTITY, proxy headers are ignored and DEV_USER_ID (local development only;
refused at startup in production, see AppConfig.check) is the identity.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

ALB_KEY_URL = "https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"


@dataclass(frozen=True)
class AuthUser:
    """Authenticated user from a verified ALB token or the dev identity."""

    user_id: str
    email: str | None = None


class InvalidToken(ValueError):
    pass


def _b64(segment: str) -> bytes:
    # ALB tokens are base64url and may carry "=" padding; normalise either way.
    segment = segment.rstrip("=")
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


_keys: dict[str, str] = {}
_keys_lock = Lock()


def fetch_alb_key(region: str, kid: str) -> str:
    """The ALB's PEM public key for a key id (cached: keys do not change for a kid)."""
    with _keys_lock:
        if kid in _keys:
            return _keys[kid]
    import httpx

    if not kid or "/" in kid or ".." in kid:
        raise InvalidToken("bad key id")
    response = httpx.get(ALB_KEY_URL.format(region=region, kid=kid), timeout=5)
    response.raise_for_status()
    with _keys_lock:
        _keys[kid] = response.text
    return response.text


def verify_alb_token(token: str, *, region: str, expected_signer: str,
                     key_for: Callable[[str, str], str] = fetch_alb_key, now: float | None = None) -> dict:
    """The claims of an ALB-signed token, or InvalidToken."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64(header_b64))
        claims = json.loads(_b64(payload_b64))
        signature = _b64(signature_b64)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidToken("malformed token") from exc
    if header.get("alg") != "ES256":
        raise InvalidToken("unexpected algorithm")
    if not expected_signer or header.get("signer") != expected_signer:
        raise InvalidToken("token not signed by the expected load balancer")
    try:
        key = serialization.load_pem_public_key(key_for(region, str(header.get("kid", ""))).encode())
    except InvalidToken:
        raise
    except Exception as exc:  # noqa: BLE001 - no key, no trust
        raise InvalidToken("signing key unavailable") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise InvalidToken("unexpected key type")
    if len(signature) == 64:  # JOSE raw r || s
        signature = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
    try:
        key.verify(signature, f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise InvalidToken("bad signature") from exc
    # The ALB puts the expiry in the signed JWT *header* (with alg, kid, signer, iss, client); the
    # payload holds only the IdP's user claims. Reading it from the payload rejected every real token
    # and ignored the real expiry (second review). The docs show it as a string, so a numeric string
    # is accepted too.
    try:
        expires = float(header.get("exp"))
    except (TypeError, ValueError):
        raise InvalidToken("token has no expiry") from None
    if expires < (now if now is not None else time.time()):
        raise InvalidToken("token expired")
    if not claims.get("sub"):
        raise InvalidToken("token has no subject")
    return claims


def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency that returns the authenticated user, or 401."""
    config = getattr(request.app.state, "config", None)
    if config is not None and getattr(config, "trust_proxy_identity", False):
        token = request.headers.get("x-amzn-oidc-data", "")
        try:
            claims = verify_alb_token(token, region=config.aws_region, expected_signer=config.alb_arn or "")
        except InvalidToken as exc:
            logger.warning("Rejected proxy identity: %s", exc)
            raise HTTPException(status_code=401, detail="Authentication required") from None
        return AuthUser(user_id=str(claims["sub"]), email=claims.get("email"))

    dev_user_id = getattr(config, "dev_user_id", None) if config else None
    if dev_user_id:
        return AuthUser(user_id=dev_user_id, email="dev@localhost")

    raise HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
