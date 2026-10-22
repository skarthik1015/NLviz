"""Identity and feedback boundaries (review B1, B15)."""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.security.auth import InvalidToken, get_current_user, verify_alb_token

ALB = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/nlviz/abc"


def _b64(data: bytes) -> str:
    # The ALB keeps base64 padding on every segment ("includes padding characters at the end").
    return base64.urlsafe_b64encode(data).decode()


def _token(key, *, signer=ALB, sub="user-1", exp=None, alg="ES256", exp_in_header=True) -> str:
    """A token in the ALB's documented shape: expiry in the header (as a string, as AWS shows it),
    the IdP's user claims (no exp) in the payload."""
    expiry = str(int(exp or time.time() + 60))
    header_fields = {"alg": alg, "kid": "k1", "signer": signer, "iss": "https://idp", "client": "c"}
    if exp_in_header:
        header_fields["exp"] = expiry
    claims = {"sub": sub, "email": "u@x"} if exp_in_header else {"sub": sub, "email": "u@x", "exp": float(expiry)}
    header = _b64(json.dumps(header_fields).encode())
    payload = _b64(json.dumps(claims).encode())
    r, s = decode_dss_signature(key.sign(f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{header}.{payload}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


@pytest.fixture(scope="module")
def key():
    return ec.generate_private_key(ec.SECP256R1())


def _pem(key) -> str:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def test_a_token_signed_by_the_expected_alb_is_accepted(key):
    claims = verify_alb_token(_token(key), region="us-east-1", expected_signer=ALB, key_for=lambda r, k: _pem(key))
    assert claims["sub"] == "user-1"


def test_only_the_signed_header_expiry_counts(key):
    """A payload exp (not where the ALB puts it) neither expires nor extends a token."""
    with pytest.raises(InvalidToken, match="no expiry"):
        verify_alb_token(_token(key, exp_in_header=False), region="us-east-1", expected_signer=ALB,
                         key_for=lambda r, k: _pem(key))


@pytest.mark.parametrize("change", ["signer", "expired", "other_key", "alg", "tampered"])
def test_anything_else_is_rejected(key, change):
    other = ec.generate_private_key(ec.SECP256R1())
    token = {
        "signer": lambda: _token(key, signer="arn:other"),
        "expired": lambda: _token(key, exp=time.time() - 1),
        "other_key": lambda: _token(other),
        "alg": lambda: _token(key, alg="none"),
        "tampered": lambda: (lambda t: t.split(".")[0] + "." + _b64(json.dumps(
            {"sub": "admin", "exp": time.time() + 60}).encode()) + "." + t.split(".")[2])(_token(key)),
    }[change]()
    with pytest.raises(InvalidToken):
        verify_alb_token(token, region="us-east-1", expected_signer=ALB, key_for=lambda r, k: _pem(key))


def _config(**overrides) -> AppConfig:
    base = dict(api_prefix="", database_url=None, auto_migrate=False, db_pool_min=1, db_pool_max=1,
                secret_backend="local", secret_prefix="x", upload_bucket=None, schema_bucket=None,
                aws_region="us-east-1", cors_origins=(), rate_limit_rpm=0, environment="development",
                log_level="INFO", dev_user_id=None)
    return AppConfig(**{**base, **overrides})


def _whoami(config: AppConfig) -> TestClient:
    from fastapi import Depends

    app = FastAPI()
    app.state.config = config

    @app.get("/me")
    def me(user=Depends(get_current_user)):
        return {"user": user.user_id}

    return TestClient(app)


def test_a_bare_identity_header_proves_nothing():
    client = _whoami(_config(dev_user_id="demo-user"))
    assert client.get("/me", headers={"x-amzn-oidc-identity": "someone-else"}).json() == {"user": "demo-user"}
    assert _whoami(_config()).get("/me", headers={"x-amzn-oidc-identity": "someone-else"}).status_code == 401
    trusting = _whoami(_config(trust_proxy_identity=True, alb_arn=ALB))
    assert trusting.get("/me", headers={"x-amzn-oidc-identity": "someone-else"}).status_code == 401


def test_unsafe_deployments_are_refused_at_startup():
    with pytest.raises(ValueError, match="DEV_USER_ID"):
        _config(environment="production", dev_user_id="demo-user").check()
    with pytest.raises(ValueError, match="ALB_ARN"):
        _config(trust_proxy_identity=True).check()
    _config(dev_user_id="demo-user").check()


def test_feedback_keys_are_per_owner_and_per_query(tmp_path):
    from app.services.feedback_store import FeedbackConflict, JsonlFeedbackStore

    store = JsonlFeedbackStore()
    store._jsonl_path = tmp_path / "feedback.jsonl"
    store._records.clear()
    store._records_by_idempotency_key.clear()

    status, mine = store.upsert(owner_id="a", query_id="q1", rating="positive", comment=None, idempotency_key="k")
    assert status == "created" and mine.owner_id == "a"
    status, theirs = store.upsert(owner_id="b", query_id="q9", rating="negative", comment="x", idempotency_key="k")
    assert status == "created" and theirs.feedback_id != mine.feedback_id  # same key, another owner: no overwrite
    assert store.upsert(owner_id="a", query_id="q1", rating="negative", comment=None, idempotency_key="k")[0] == "updated"
    with pytest.raises(FeedbackConflict):
        store.upsert(owner_id="a", query_id="q2", rating="negative", comment=None, idempotency_key="k")


def test_feedback_requires_a_user():
    from app.api.routes.feedback import router
    from app.dependencies import get_feedback_store

    app = FastAPI()
    app.state.config = _config()
    app.include_router(router)
    app.dependency_overrides[get_feedback_store] = lambda: None
    response = TestClient(app).post("/feedback", json={"query_id": "q", "rating": "positive"})
    assert response.status_code == 401
