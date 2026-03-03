from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import build_api_router
from app.dependencies import build_runtime_services


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = build_runtime_services()
    app.state.connector = services["connector"]
    app.state.semantic_registry = services["semantic_registry"]
    app.state.query_graph = services["query_graph"]
    app.state.query_service = services["query_service"]
    app.state.feedback_store = services["feedback_store"]
    try:
        yield
    finally:
        app.state.connector.close()


app = FastAPI(title="NL Query Tool API", version="0.1.0", lifespan=lifespan)
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(build_api_router())


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
