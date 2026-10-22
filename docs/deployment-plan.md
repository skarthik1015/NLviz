# NLviz deployment plan (ready, not started)

**Status:** documented, not done. The project runs as a local Docker Compose demo.

## Decisions already made

| Question | Decision |
|---|---|
| LLM in the deployment | Hosted `gpt-4.1-mini` (measured in design doc §19: more accurate and faster than the local 4B model, about $0.001 per question). Spend limit on the provider account. Ollama stays the default for local development. |
| Who can use it | Private, invite only (allow-list or shared password); no anonymous uploads or database connections. |
| Old `.github/workflows/deploy.yml` (v1: paid OpenAI gate + AWS deploy on every push to `main`) | Replace when deploying, with a workflow for the chosen target. Until then, do not push to `main` without removing or disabling it. |
| CI | `.github/workflows/ci.yml` (offline tests + frontend typecheck, free). Push with the next push. |
| Budget | Not set yet. |

## Shape (to confirm when budget is set)

- One small VM (about 4 GB RAM) running the existing Docker Compose stack: jammi-server, the app's Postgres, backend, frontend, behind a reverse proxy with TLS.
- Secrets from the host environment, never the repo (`SECRET_STORE_KEY`, `JAMMI_SERVICE_TENANT`, `OPENAI_API_KEY`).
- Volumes for uploads, published schemas and the app database; nightly backup of the app database and schemas.
- Per-user rate limits (Phase 6), request logging without user data, health check on `/healthz`.

## Before going live

1. Phase 6 hardening pass (security-and-hardening skill): auth on every route, upload limits, rate limits, error messages, dependency audit.
2. Re-run the full eval set on the hosted model (results log, design doc §19–20 procedure).
3. Re-check the engine-compatibility protections (see the architecture doc) on the deployed engine version.
