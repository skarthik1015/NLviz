import os

from fastapi import APIRouter

from .routes import auth_router, chat_router, connections_router, feedback_router, schema_router


def build_api_router() -> APIRouter:
    api_router = APIRouter()
    api_router.include_router(auth_router)
    api_router.include_router(chat_router)
    api_router.include_router(schema_router)
    api_router.include_router(feedback_router)
    api_router.include_router(connections_router)
    if os.getenv("BENCH_ENDPOINTS", "off").strip().lower() == "on":
        # The text-to-SQL baseline for the ablation (docs/ablation-results.md). Measuring only.
        if os.getenv("ENVIRONMENT", "development").lower() in {"prod", "production"}:
            raise RuntimeError("BENCH_ENDPOINTS must not be on in production")
        from .routes.bench import router as bench_router

        api_router.include_router(bench_router)
    return api_router
