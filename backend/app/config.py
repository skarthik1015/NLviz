"""Application configuration loaded from environment variables.

AppConfig holds the app's settings (API, storage, auth, limits, Jammi) and can be built directly in
tests. Not every variable is read here: the LLM settings (LLM_PROVIDER, LLM_MODEL, per-task
LLM_PROVIDER_<TASK> / LLM_MODEL_<TASK>, INTENT_MODE...) are read by IntentMapperConfig.from_env, the
secret store's key by app.services.secret_store, and the upload size limit by the connection
routes. AppConfig.check refuses unsafe deployments at startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


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
    """Maximum questions per minute per user (all question routes)."""

    # ── Misc ─────────────────────────────────────────────────────────
    environment: str
    """Deployment environment label, e.g. 'dev', 'prod'."""

    log_level: str
    """Python logging level string, e.g. 'INFO', 'DEBUG'."""

    # ── Auth ─────────────────────────────────────────────────────────
    dev_user_id: str | None
    """When set, bypasses ALB/Cognito auth and uses this as the user ID.
    Only for local development — never set in deployed environments."""

    # LLM settings (LLM_PROVIDER, LLM_MODEL, per-task overrides) are read by IntentMapperConfig;
    # providers live in app/llm.py.

    # ── Jammi (query engine) ─────────────────────────────────────────
    jammi_url: str | None = None
    """jammi-server gRPC/Flight address, e.g. 'grpc://jammi:8081'. None = legacy connectors only."""

    jammi_service_tenant: str | None = None
    """The one Jammi tenant NLviz binds every connection to (UUID v4/v7; decision AD-5)."""

    jammi_demo_data_url: str | None = None
    """Directory of the bundled Olist Parquet files AS THE JAMMI SERVER SEES IT, e.g. '/data/olist'.
    When set with jammi_url, the shared 'default' demo connection is served by Jammi."""

    jammi_upload_dir: str | None = None
    """Local directory NLviz writes uploaded files to (used when upload_bucket is not set)."""

    jammi_upload_url: str | None = None
    """The same directory AS THE JAMMI SERVER SEES IT, e.g. '/uploads' for a shared Compose volume."""

    mysql_federation: bool = False
    """Query MySQL live through Jammi instead of importing a snapshot. Off by default: Jammi's
    MySQL pushdown returns wrong results for some SQL (issue J27)."""

    mysql_snapshot_max_rows: int = 5_000_000
    """Largest MySQL database (total rows) a snapshot import accepts."""

    grounding_enabled: bool = True
    # Retrieved few-shot examples (Phase 3d); off gives the static examples (for A/B evals).
    examples_enabled: bool = True
    """Value grounding (Phase 2): index each connection's values on Jammi and use them in intent
    mapping. Off only for before/after evaluation."""

    s3_import_allow_custom_endpoint: bool = False
    """Let users import from S3-compatible stores at their own endpoint URL. Off by default: the
    backend would fetch a user-chosen URL (SSRF)."""

    max_concurrent_questions: int = 2
    """Questions one user may have running at once (each holds LLM and engine work)."""

    trust_proxy_identity: bool = False
    """Behind an ALB with OIDC: take the user from the ALB-signed x-amzn-oidc-data token (verified).
    Off: proxy identity headers are ignored (anyone reaching the backend could set them)."""

    alb_arn: str | None = None
    """The ALB expected as the token's signer; required with trust_proxy_identity."""

    def check(self) -> None:
        """Refuse configurations that would be unsafe if deployed."""
        if self.environment.lower() in {"prod", "production"} and self.dev_user_id:
            raise ValueError("DEV_USER_ID must not be set in production: it signs every request in as one user")
        if self.trust_proxy_identity and not self.alb_arn:
            raise ValueError("TRUST_PROXY_IDENTITY needs ALB_ARN, the load balancer that signs the identity")

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
            dev_user_id=os.getenv("DEV_USER_ID") or None,
            jammi_url=os.getenv("JAMMI_URL") or None,
            jammi_service_tenant=os.getenv("JAMMI_SERVICE_TENANT") or None,
            jammi_demo_data_url=os.getenv("JAMMI_DEMO_DATA_URL") or None,
            jammi_upload_dir=os.getenv("JAMMI_UPLOAD_DIR") or None,
            jammi_upload_url=os.getenv("JAMMI_UPLOAD_URL") or None,
            mysql_federation=os.getenv("MYSQL_FEDERATION", "false").lower() in ("1", "true", "yes"),
            mysql_snapshot_max_rows=int(os.getenv("MYSQL_SNAPSHOT_MAX_ROWS", "5000000")),
            grounding_enabled=os.getenv("GROUNDING_ENABLED", "true").lower() in ("1", "true", "yes"),
            examples_enabled=os.getenv("EXAMPLES_ENABLED", "true").lower() in ("1", "true", "yes"),
            s3_import_allow_custom_endpoint=os.getenv("S3_IMPORT_ALLOW_CUSTOM_ENDPOINT", "false").lower()
            in ("1", "true", "yes"),
            max_concurrent_questions=int(os.getenv("MAX_CONCURRENT_QUESTIONS", "2")),
            trust_proxy_identity=os.getenv("TRUST_PROXY_IDENTITY", "false").lower() in ("1", "true", "yes"),
            alb_arn=os.getenv("ALB_ARN") or None,
        )
