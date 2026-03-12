"""Centralised application configuration loaded from environment variables.

All env-var reads are isolated here so that:
- No module has scattered os.getenv() calls.
- Configuration can be injected / overridden in tests via AppConfig(…).
- The set of supported env vars is documented in one place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AppConfig:
    # ── API ──────────────────────────────────────────────────────────
    api_prefix: str
    """URL prefix for all API routes, e.g. '/api'. Empty string = no prefix."""

    # ── Database ─────────────────────────────────────────────────────
    database_url: str | None
    """Full PostgreSQL DSN, e.g. postgresql://user:pass@host:5432/dbname.
    When None, file-based JSONL stores are used (local dev mode)."""

    auto_migrate: bool
    """If True and database_url is set, run pending migrations at startup."""

    db_pool_min: int
    """Minimum connections in the PostgreSQL connection pool."""

    db_pool_max: int
    """Maximum connections in the PostgreSQL connection pool."""

    # ── Secrets ──────────────────────────────────────────────────────
    secret_backend: str
    """Storage backend for per-connection credentials.
    Accepted values: 'local' (Fernet-encrypted files) | 'aws_secrets_manager'."""

    secret_prefix: str
    """Prefix for AWS Secrets Manager secret names, e.g. 'nl-query-tool'."""

    # ── S3 ───────────────────────────────────────────────────────────
    upload_bucket: str | None
    """S3 bucket for uploaded CSV/Parquet files. None = store locally."""

    schema_bucket: str | None
    """S3 bucket for auto-generated semantic YAML files. None = store locally."""

    aws_region: str
    """AWS region used for S3 and Secrets Manager clients."""

    # ── HTTP / Middleware ─────────────────────────────────────────────
    cors_origins: tuple[str, ...]
    """Allowed CORS origins."""

    rate_limit_rpm: int
    """Maximum requests per minute per client IP."""

    # ── Misc ─────────────────────────────────────────────────────────
    environment: str
    """Deployment environment label, e.g. 'dev', 'prod'."""

    log_level: str
    """Python logging level string, e.g. 'INFO', 'DEBUG'."""

    # ── LLM ──────────────────────────────────────────────────────────
    anthropic_model: str
    """Claude model ID used for intent mapping."""

    # ── Class methods ────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build config from environment variables.

        Called once at application startup inside the FastAPI lifespan handler.
        """
        raw_origins = os.getenv(
            "CORS_ALLOW_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        )
        origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())

        return cls(
            api_prefix=os.getenv("API_PREFIX", ""),
            database_url=os.getenv("DATABASE_URL") or None,
            auto_migrate=os.getenv("AUTO_MIGRATE", "false").lower() in ("1", "true", "yes"),
            db_pool_min=int(os.getenv("DB_POOL_MIN", "1")),
            db_pool_max=int(os.getenv("DB_POOL_MAX", "10")),
            secret_backend=os.getenv("SECRET_BACKEND", "local"),
            secret_prefix=os.getenv("SECRET_PREFIX", "nl-query-tool"),
            upload_bucket=os.getenv("UPLOAD_BUCKET") or None,
            schema_bucket=os.getenv("SCHEMA_BUCKET") or None,
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            cors_origins=origins,
            rate_limit_rpm=int(os.getenv("RATE_LIMIT_RPM", "30")),
            environment=os.getenv("ENVIRONMENT", "development"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        )
