from contextlib import asynccontextmanager
import importlib
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import build_api_router
from app.config import AppConfig
from app.dependencies import build_all_services
from app.rate_limit import QueryLimiter
from app.observability import Observability, configure as configure_observability
from app.observability.health import health_details
from app.observability.middleware import QuestionLoggingMiddleware, capture_authenticated_user
from app.observability.service import configure_json_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_runtime_dependencies()

    config = AppConfig.from_env()
    config.check()

    # ── Configure logging ─────────────────────────────────────────────
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    configure_json_logging()

    # ── Optional infrastructure (absent in local dev) ─────────────────
    pool = None
    if config.database_url:
        from app.storage.db_pool import DatabasePool
        pool = DatabasePool(
            dsn=config.database_url,
            minconn=config.db_pool_min,
            maxconn=config.db_pool_max,
        )
        if config.auto_migrate:
            from migrations.migrate import run_migrations
            run_migrations(pool)

    upload_storage = None
    if config.upload_bucket:
        from app.storage.s3_storage import S3Storage
        upload_storage = S3Storage(bucket=config.upload_bucket, region=config.aws_region)

    schema_storage = None
    if config.schema_bucket:
        from app.storage.s3_storage import S3Storage as _S3
        from app.storage.schema_storage import S3SchemaStorage
        schema_s3 = _S3(bucket=config.schema_bucket, region=config.aws_region)
        schema_storage = S3SchemaStorage(s3=schema_s3)
    elif pool is None:
        # Local dev — use filesystem-backed schema storage
        from app.storage.schema_storage import LocalSchemaStorage
        schema_storage = LocalSchemaStorage()

    # ── Build all services ────────────────────────────────────────────
    services = build_all_services(
        config=config, pool=pool, schema_storage=schema_storage, upload_storage=upload_storage
    )

    app.state.config = config
    app.state.database_pool = pool
    app.state.observability = Observability.from_env(pool=pool)
    # app.llm meters every provider attempt (raw, structured and each Instructor retry, SDK retries
    # off): tests/test_llm_spend_hooks.py. The cap is enforced.
    app.state.observability.hooks_integrated = True
    configure_observability(app.state.observability)
    app.state.upload_storage = upload_storage
    app.state.schema_storage = schema_storage
    # Core services
    app.state.connection_service = services["connection_service"]
    app.state.connection_store = services["connection_store"]
    app.state.secret_store = services["secret_store"]
    app.state.job_manager = services["job_manager"]
    app.state.audit_log = services["audit_log"]
    app.state.feedback_store = services["feedback_store"]
    app.state.intent_config = services["intent_config"]
    app.state.jammi_provisioner = services["jammi_provisioner"]
    app.state.jammi_upload_store = services["jammi_upload_store"]
    app.state.grounding = services["grounding"]
    app.state.checkpointer = services["checkpointer"]
    # Backward compat: expose default runtime's services directly
    app.state.connector = services["connector"]
    app.state.schema_context = services["schema_context"]
    app.state.semantic_registry = services["semantic_registry"]
    app.state.query_graph = services["query_graph"]
    app.state.query_service = services["query_service"]

    logger.info(
        "Startup complete — env=%s secret_backend=%s db=%s upload_bucket=%s",
        config.environment,
        config.secret_backend,
        "postgres" if pool else "none",
        config.upload_bucket or "local",
    )

    try:
        yield
    finally:
        configure_observability(None)
        if hasattr(app.state, "job_manager"):
            app.state.job_manager.shutdown()
        if getattr(app.state, "grounding", None) is not None:
            app.state.grounding.shutdown()
        if getattr(app.state, "checkpointer", None) is not None:
            app.state.checkpointer.close()
        if hasattr(app.state.connector, "close"):
            app.state.connector.close()
        if pool:
            pool.close()


def _ensure_runtime_dependencies() -> None:
    try:
        importlib.import_module("multipart")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            'Form uploads require "python-multipart" to be installed. '
            "Run: pip install python-multipart"
        ) from exc


config = AppConfig.from_env()

app = FastAPI(title="NL Query Tool API", version="0.1.0", lifespan=lifespan)

# Per-user question limits, applied by the question routes once the user is known.
app.state.query_limiter = QueryLimiter(requests_per_minute=config.rate_limit_rpm,
                                       max_concurrent=config.max_concurrent_questions)
app.add_middleware(
    QuestionLoggingMiddleware,
    api_prefix=config.api_prefix,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(build_api_router(), prefix=config.api_prefix,
                   dependencies=[Depends(capture_authenticated_user)])
app.add_api_route("/health/details", health_details, methods=["GET"])


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
