from contextlib import asynccontextmanager
import importlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import build_api_router
from app.config import AppConfig
from app.dependencies import build_all_services
from app.rate_limit import RateLimitMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_runtime_dependencies()

    config = AppConfig.from_env()

    # ── Configure logging ─────────────────────────────────────────────
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))

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
    services = build_all_services(config=config, pool=pool, schema_storage=schema_storage)

    app.state.config = config
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
        if hasattr(app.state, "job_manager"):
            app.state.job_manager.shutdown()
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

# NOTE: Starlette middleware is LIFO — last added runs outermost.
# RateLimitMiddleware must be added first so CORS headers are always present,
# even on 429 responses (otherwise browsers report NetworkError instead of 429).
app.add_middleware(
    RateLimitMiddleware,
    requests_per_minute=config.rate_limit_rpm,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(build_api_router(), prefix=config.api_prefix)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
