# NL Query Tool

Natural-language analytics MVP with a deterministic semantic SQL compiler.

The current repo contains:
- a FastAPI backend that maps a question to `SemanticIntent`, compiles SQL from a governed semantic YAML, validates SQL safety, and executes against DuckDB
- a LangGraph skeleton that orchestrates `intent_mapper -> sql_builder -> executor`
- a minimal Next.js frontend that calls `/chat`, renders a results table, and shows a derived plot spec
- deterministic backend tests, including a 10-case golden suite and SQL safety coverage

## Current Status

Implemented now:
- DuckDB-backed backend MVP
- Semantic layer loader and deterministic SQL builder
- SQL safety validation with `sqlglot`
- LangGraph execution skeleton
- Minimal frontend chat page
- CI test workflow for backend and frontend checks

Not implemented yet:
- LLM-backed intent mapping
- backend-owned chart selector/spec generator
- persistent query history / feedback storage
- infrastructure and deployment workflows

## Repo Structure

```text
nl-query-tool/
|- backend/              FastAPI app, semantic compiler, tests, Dockerfile
|- frontend/             Next.js app, minimal chat workbench, Dockerfile
|- docs/                 Current architecture and repo guidance
|- .github/workflows/    CI checks
`- docker-compose.yml    Local multi-container runtime
```

## Prerequisites

For local development without Docker:
- Python 3.11
- Node.js 20+
- npm 10+

For containerized development:
- Docker Desktop with Compose support

## Backend Local Run

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python seed.py
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API endpoints:
- `GET /healthz`
- `GET /schema`
- `POST /chat`
- `POST /feedback`

## Frontend Local Run

```bash
cd frontend
npm install
set NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
npm run dev
```

Open `http://localhost:3000`.

## Docker Run

The compose setup expects the source dataset CSVs in `backend/data/raw/`.

1. Seed the DuckDB file:

```bash
docker compose build backend
docker compose run --rm backend python seed.py
```

2. Start the app:

```bash
docker compose up --build
```

Services:
- frontend: `http://localhost:3000`
- backend: `http://localhost:8000`

## Tests

Backend:

```bash
cd backend
pytest
```

Frontend:

```bash
cd frontend
npm install
npm run typecheck
npm run build
```

## Environment Variables

Backend:
- `CORS_ALLOW_ORIGINS` comma-separated list, defaults to `http://localhost:3000,http://127.0.0.1:3000`

Frontend:
- `NEXT_PUBLIC_API_BASE_URL` defaults to `http://localhost:8000`

## Notes

- The current golden suite is deterministic and designed around the heuristic mapper. When the LLM mapper replaces it, update the golden cases rather than deleting them.
- The frontend currently derives a plot spec client-side. The planned production architecture moves chart selection to the backend.
- The architecture document at `docs/ARCHITECTURE.md` describes the current implemented system and the next planned increments.