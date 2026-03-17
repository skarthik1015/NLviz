# NL Query Tool

Natural-language analytics MVP with a deterministic semantic SQL compiler.

The current repo contains:
- a FastAPI backend that maps a question to `SemanticIntent`, compiles SQL from a governed semantic YAML, validates SQL safety, and executes against DuckDB
- a LangGraph pipeline that orchestrates `intent_mapper -> sql_builder -> executor -> validator -> chart_selector -> explainer`
- a minimal Next.js frontend that calls `/chat`, renders a results table, and shows a derived plot spec
- deterministic backend tests, including a 20-case golden suite with an 80% accuracy gate and SQL safety coverage

## Current Status

Implemented now:
- DuckDB-backed backend MVP
- Semantic layer loader and deterministic SQL builder
- SQL safety validation with `sqlglot`
- LangGraph execution pipeline with validation, chart selection, and explanation
- Minimal frontend chat page
- CI test workflow for backend and frontend checks

Not implemented yet:
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

1. Start the app (first boot auto-seeds DuckDB if missing):

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
- `DEV_USER_ID` optional local-development bypass for ALB/Cognito auth; set this to a stable user id when testing personal database connections locally

Frontend:
- `NEXT_PUBLIC_API_BASE_URL` defaults to `http://localhost:8000`

## Deploy Notes

For the current dev setup:
- run `terraform apply` in `infrastructure/terraform/environments/dev` to provision the ALB, ECS services, RDS, and S3-backed connection workflow
- access the app over the ALB DNS name output by Terraform; a custom domain is not required
- the hosted backend uses `backend_dev_user_id` from [terraform.tfvars](c:/Lace/NLviz/nl-query-tool/infrastructure/terraform/environments/dev/terraform.tfvars) as a dev auth bypass until Cognito is re-enabled
- set `DEV_USER_ID=<your-id>` locally when developing without the ALB/Cognito flow

## Notes

- The golden suite is deterministic and validates semantic compilation and orchestration behavior; keep cases aligned with supported intent patterns.
- The intent mapper runs LLM-first with heuristic fallback for resiliency.
- The frontend shows `Invalid/ Unsafe Query` for blocked or invalid requests.
- The architecture document at `docs/ARCHITECTURE.md` describes the current implemented system and the next planned increments.
