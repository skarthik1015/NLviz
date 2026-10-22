# Freeze manifest (frozen 2026-10-20, before the sealed holdout)

The sealed holdout is run once, against exactly what is recorded here. The review gate closed
on 2026-10-20. No code change before the holdout run.

## Application

| Item | Value |
|---|---|
| Code | The development snapshot of 2026-10-20 (application code unchanged since the last review fix). The published history is squashed, so that snapshot is not a separate commit here; the changes made after it are listed below |
| Answer path | `ANSWER_PATH=sql` (SQL-primary, the default since `8e2a5fb`) |
| Other settings | `PHRASE_REWRITES=on` (compiler path only), `SQL_FALLBACK=off`, `BENCH_ENDPOINTS=on` (needed by the runner for the baseline arm) |
| Engine | `ghcr.io/f-inverse/jammi-ai-server:0.54.0@sha256:6e98b1735cda…` |
| Code fingerprint | backend image `sha256:1f42b993b740…`; the runner also records code fingerprints per run. Offline tests at freeze: 606 passed |

**Harness fix after the freeze (2026-10-20, before any holdout question was asked).** The first
`--final-evaluation` attempt stopped after setup with a `TypeError`, and no case was asked (no `/chat`
request reached the backend). The holdout runner wraps `run.ask_turn` with its original six-argument
signature, and `run.py` had since started passing a seventh (the guarded arm's extra fields). Fix:
`run.py` passes it only to endpoints that have extra fields. No application code changed. The run
was repeated under the same arm label (no result file had been written).

**After the freeze** (not part of the holdout numbers): definition clarifications and their review
fixes, the "Try the sample data" entry point, two presentation fixes (chart title, query metadata),
and documentation. The development-set results for these are in `docs/ablation-results.md`. The tag
`v2.0-rc` marks the published v2 release candidate, which includes them.

## Models

| Run | Provider and model | All tasks? |
|---|---|---|
| Local | Ollama `qwen3:4b-instruct-8k` | yes (schema generation, SQL, answerability, explanations) |
| Hosted | OpenAI `gpt-4.1-mini` | yes |

**As run:** the runner reuses the connections in its state. The local run generated the holdout's
semantic layers with qwen3:4b, and the hosted run answered on those same layers, as the
development-set hosted runs did. The layer fingerprints are in each answer's `audit.run.schema`.

**Runs:**
- `final-local`: 2026-10-20.
- `final-hosted`: 2026-10-20, $0.26.

Results are in `docs/ablation-results.md`.

## Arms (holdout runner `--endpoint both`)

| Arm | Endpoint | What runs |
|---|---|---|
| SQL-primary | `/chat` | The app as shipped: guarded SQL held to the semantic layer, with clarifications |
| Text-to-SQL baseline | `/bench/sql` (unguarded) | The model writes SQL from the same context, with no checks: the Phase 0 baseline |

Earlier results named "NLviz" were the **compiler path** (`/chat` before `8e2a5fb`). They are not
comparable under that name.

## Scoring and runner

| Item | Value |
|---|---|
| Repository scorer | `backend/eval/bench/score.py` at the frozen commit, SHA-256 `05543dd72edad898…` (whole numbers exact, seven first-response outcomes plus final correctness) |
| Sealed scorer | the holdout's own, per `K:/NLviz2/holdout/RUNNER-README.md`; both totals are reported |
| Runner | `K:/NLviz2/holdout/run_holdout.sh`, checked against `RUNNER-SHA256SUMS` (SHA-256 of that file: `83a8a6ed5b2fb53f…`) |
| Seal | `docs/holdout-SHA256SUMS` matches the private `SHA256SUMS` (checked at freeze) |

## Commands (the runner prints only group totals)

```bash
# Local
bash /k/NLviz2/holdout/run_holdout.sh --final-evaluation \
  --ro-password-file 'K:/NLviz2/secrets/nlviz_ro_password.txt' --arm final-local
# Hosted: only after the cost estimate is approved. The app cap
# (LLM_MONTHLY_BUDGET_USD) = the month's spend so far + the approved amount.
bash /k/NLviz2/holdout/run_holdout.sh --final-evaluation \
  --ro-password-file 'K:/NLviz2/secrets/nlviz_ro_password.txt' --arm final-hosted
```

Results are published as measured in `docs/ablation-results.md`, with sample sizes and intervals.
