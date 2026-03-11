from fastapi import APIRouter

from .routes import chat_router, connections_router, feedback_router, schema_router


def build_api_router() -> APIRouter:
    api_router = APIRouter()
    api_router.include_router(chat_router)
    api_router.include_router(schema_router)
    api_router.include_router(feedback_router)
    api_router.include_router(connections_router)
    return api_router
