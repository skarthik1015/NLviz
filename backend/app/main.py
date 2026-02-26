from fastapi import FastAPI

from app.api import build_api_router

app = FastAPI(title="NL Query Tool API", version="0.1.0")
app.include_router(build_api_router())


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
