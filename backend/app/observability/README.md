# Release operations

This package answers: which question failed and how long did it take; which model
attempts consumed tokens; what spend is committed/reserved this UTC month; and
whether the app database and Jammi respond. `main.py` installs the service,
question middleware, existing authentication dependency and `/health/details`.
No provider SDK or LLM functions are monkeypatched.

**Integrated (2026-10-17):** `app.llm` meters every provider attempt (`metered_attempt`):
the raw OpenAI, Anthropic and Bedrock clients, and Instructor's structured calls with each retry
as its own attempt. SDK retries are off, the output limit is always sent, the prompt bound is the
request's UTF-8 size plus per-message overhead, and `BudgetExceeded` is never caught to try
another hosted call (it maps to HTTP 503 `LLM_BUDGET_EXCEEDED`). `main.py` sets
`hooks_integrated`. Tests: `backend/tests/test_llm_spend_hooks.py`. The integration contract
below is kept as the reference.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_MONTHLY_BUDGET_USD` | unset | Nonnegative USD cap; unset disables admission limits; zero blocks hosted calls once hooks are integrated. |
| `LLM_PRICE_TABLE_JSON` | built-in mini rates | JSON object keyed by `provider/model`, values `{"input":"0.40","output":"1.60"}` per million text tokens. Overrides/adds entries. |
| `LLM_SPEND_SQLITE_PATH` | `data/llm-spend.sqlite3` | Local persistent ledger when no app database pool exists. Mount a persistent volume. Ignored when an app pool exists. |
| `INTENT_DEBUG_LOGGING` | false | Existing opt-in also permits logging at most 4096 characters of question/resume text. Keep disabled for ordinary operation. |

The built-in standard GPT-4.1-mini and dated snapshot prices are $0.40 input and
$1.60 output per million tokens, verified on 2026-10-17 against the
[official model page](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
This is standard uncached text pricing, not an invoice reconciliation system:
cached-input discounts, tools, image/audio and batch rates need explicit extensions.
Unknown hosted models have unknown cost and fail closed when a cap is set.
Explicit local providers, or OpenAI-compatible loopback/localhost/ollama/
host.docker.internal URLs, bypass the hosted cap; pass the actual base URL.
Other custom endpoints remain classified as hosted.

The app database pool creates `llm_spend_months` and `llm_spend_calls` on startup;
the app role needs CREATE TABLE in its app schema. Monthly rows lock with
`FOR UPDATE`, serializing reservations across workers/replicas. SQLite uses
`BEGIN IMMEDIATE` across processes sharing one local file, and is not a
multi-host production store. Do not silently fall back from an unavailable
configured app database. The ledger contains month, random call id, reserved and
spent decimal USD only. Unsettled reservations survive restarts and do not expire:
reconcile them against provider billing before any manual correction. Historical
months remain for reconciliation. New UTC months start independently, and calls
crossing a month boundary settle against their admission month.

## Integration in app.llm

Use the process-wide `app.observability.before_call` / `after_call` API installed
by the main lifespan. Call it at the **actual billable attempt** boundary:

```python
call = before_call(
    provider=provider, model=model, base_url=actual_base_url,
    prompt_tokens_upper_bound=verified_input_bound,
    max_completion_tokens=provider_enforced_output_limit,
)
try:
    response = actual_provider_attempt(...)  # SDK retries disabled or wrapped individually
except BaseException:
    after_call(call, failed=True)  # unknown billing charges full reservation
    raise
else:
    after_call(call, prompt_tokens=actual_input, completion_tokens=actual_output)
```

1. Cover OpenAI raw completions, Anthropic raw messages, Bedrock converse, and
   OpenAI/Anthropic Instructor structured completions. Any additional provider,
   schema job and task override must cross the same boundary.
2. SDK internal retries must be disabled or separately admitted/accounted. Every
   Instructor repair/retry is a new call, with its expanded prompt and new reserve.
   Instructor `completion:response` alone is too late to enforce admission, and
   wrapping the whole Instructor invocation once undercounts retries.
3. Establish a real prompt token upper bound (including messages, tools/system,
   repair conversation and provider overhead). An estimate below actual usage
   cannot enforce a hard cap. Use a verified tokenizer plus overhead or the model
   context maximum as a conservative bound. Enforce the supplied completion limit
   in the actual SDK request; include reasoning tokens where billed. Do not catch
   `BudgetExceeded` and fall back to another hosted call. After a successful provider
   response, account before parsing/validation so rejected responses count too.
4. On network/provider failure with no usage, settle with missing counts: the full
   reservation is conservatively charged. If provider usage exists on an error,
   supply it and `failed=True`. Crashes leave reserves committed. Do not release
   uncertain spend to make another retry fit. Settlement is idempotent for hosted
   priced calls. Actual usage over a supplied bound is recorded truthfully, even
   if over cap; that indicates an integration error, not a successful hard limit.
5. Map `BudgetExceeded` to a clear HTTP/SSE budget error at route error handling;
   current route handlers map arbitrary exceptions to a generic internal error.
   Propagate the error through mapper retry/fallback logic without swallowing it.
6. Only after all provider-attempt paths are tested, set `hooks_integrated=True`
   for the configured service. Add integration tests proving blocked raw,
   structured, SDK retry, parallel answerability and schema calls make no provider
   request, and local calls still work. This package's tests exercise the primitives,
   not that currently absent provider integration.

`before_call` captures the current question context. Preserve it with
`contextvars.copy_context()` when starting custom thread pools. Calls outside HTTP
still reserve globally and emit per-call JSON without a request id. Pass the same
Call to `after_call` once per actual attempt. Avoid double-counting existing audit
meters: audits are a log fallback only and never mutate spend.

## Logs, health and limits

Dedicated `nlviz.questions` JSON lines include entry point, generated/validated
request id (also returned in `X-Request-ID`), safe query id, SHA-256 user reference,
connection id, answer/HTTP status, latency, model list and token/cost totals.
Identity comes from the cached existing `get_current_user` dependency; emails,
identity headers, authorization, cookies, SQL, rows, prompts and explanations are
excluded. User references are pseudonymous identifiers: restrict log access and
set retention/deletion in the deployment log sink before release. Debug question
logging deliberately increases exposure and should only be enabled briefly.

JSON and SSE are forwarded unchanged; middleware inspects at most 1 MiB per JSON
response or pending SSE event. Oversized responses mark observation incomplete.
No external response is forced into debug mode. Until per-call hooks are wired,
only debug audit `llm_calls` can supply usage; normal responses correctly report
unknown tokens/cost. A zero/missing call list is not proof that no paid call occurred.
Audits cannot reliably classify local OpenAI-compatible endpoints without their
base URL, so unknown price stays unknown. Per-call hooks resolve that limitation.

`GET /health/details` uses the API's authentication dependency and returns no
DSNs, keys, endpoint URLs or exception text. Probes run off the event loop with a
three-second response timeout and at most one outstanding probe per process.
Configured dependencies return reachable/unreachable, absent ones not_configured.
The LLM check reports configuration, never makes a paid test call. No metrics
backend, alert delivery or production log sink is added in this package.

Offline checks: `python -m pytest -q app/observability` from backend.
