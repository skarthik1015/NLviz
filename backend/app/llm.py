"""The only module that knows about LLM providers.

Everything else asks for a completion through `build_completion_client(provider)` (raw JSON) or
`structured_completion(...)` (a Pydantic object via Instructor), with the provider and model taken
from configuration, so models can be swapped without code changes:

- local (Ollama, or any OpenAI-compatible server): LLM_PROVIDER=openai, OPENAI_BASE_URL=http://host:11434/v1,
  OPENAI_API_KEY=anything, LLM_MODEL=qwen3:4b-instruct-8k;
- OpenAI: LLM_PROVIDER=openai, OPENAI_API_KEY=sk-..., LLM_MODEL=gpt-4.1-mini (no OPENAI_BASE_URL);
- Anthropic: LLM_PROVIDER=anthropic, ANTHROPIC_API_KEY=..., LLM_MODEL=claude-sonnet-4-5;
- Bedrock: LLM_PROVIDER=bedrock (AWS credentials), LLM_MODEL=<model id>.

Each task can use its own provider/model (IntentMapperConfig.for_task): LLM_PROVIDER_<TASK> and
LLM_MODEL_<TASK> for TASK in INTENT, ANSWERABILITY, EXPLAIN, SCHEMA, e.g. a hosted model for schema
generation and the local one for questions.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

Provider = Literal["anthropic", "openai", "bedrock"]
TASKS = ("intent", "answerability", "explain", "schema")
DEFAULT_MAX_TOKENS = 800

T = TypeVar("T", bound=BaseModel)


class LLMError(ValueError):
    """The LLM could not be called or returned nothing usable."""


class LLMCompletionClient(Protocol):
    def complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_ms: int,
                      max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        ...


# The calls made for the current question, when a meter is open (`usage_meter`). A shared list:
# threads that copy the context (LangGraph nodes, the answerability pool) append to the same one.
_USAGE: ContextVar[list[dict] | None] = ContextVar("llm_usage", default=None)


@contextmanager
def usage_meter() -> Iterator[list[dict]]:
    """Collects {provider, model, prompt_tokens, completion_tokens} for each LLM call made inside."""
    calls: list[dict] = []
    token = _USAGE.set(calls)
    try:
        yield calls
    finally:
        _USAGE.reset(token)


def _record_usage(provider: str, model: str, prompt, completion) -> None:
    logger.info("LLM usage provider=%s model=%s prompt_tokens=%s completion_tokens=%s", provider, model,
                prompt, completion)
    calls = _USAGE.get()
    if calls is not None:
        calls.append({"provider": provider, "model": model, "prompt_tokens": prompt, "completion_tokens": completion})


# ── Spend: every billable provider attempt is admitted and accounted (app.observability) ──────────
# The admission bound must not underestimate: every BPE token covers at least one byte of the text
# it encodes, so the UTF-8 size of the request bounds its prompt tokens; plus room for role and
# formatting tokens per message.
_MESSAGE_OVERHEAD_TOKENS = 64


def prompt_token_bound(*parts) -> int:
    texts = [p if isinstance(p, str) else json.dumps(p, default=str, ensure_ascii=False) for p in parts if p]
    return sum(len(t.encode("utf-8")) for t in texts) + _MESSAGE_OVERHEAD_TOKENS * (len(texts) + 1)


def _observability():
    """The installed spend service, or None outside the app (tests, scripts)."""
    from app.observability import service

    return service if getattr(service, "_service", None) is not None else None


def _budget_refusal(exc: BaseException):
    """The spending cap's refusal inside an Instructor/tenacity retry error (Instructor retries a
    refused attempt, each refused again without a request, then raises its own error), or None."""
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if is_budget_exceeded(current):
            return current
        last = getattr(current, "last_attempt", None)
        if last is not None and hasattr(last, "exception"):
            pending.append(last.exception())
        for attempt in getattr(current, "failed_attempts", None) or ():
            pending.append(getattr(attempt, "exception", None))
        pending += [current.__cause__, current.__context__]
    return None


def is_budget_exceeded(exc: BaseException) -> bool:
    """A refusal by the spending cap: never caught to try another hosted call."""
    from app.observability.budget import BudgetExceeded

    return isinstance(exc, BudgetExceeded)


def _usage_counts(response, provider: str) -> tuple[int | None, int | None]:
    if provider == "bedrock":
        usage = (response.get("usage") or {}) if isinstance(response, dict) else {}
        return usage.get("inputTokens"), usage.get("outputTokens")
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    prompt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
    completion = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
    return prompt, completion


def metered_attempt(*, provider: str, model: str, base_url: str | None, prompt_bound: int,
                    max_completion_tokens: int, attempt):
    """One billable provider attempt: admitted against the monthly cap first (BudgetExceeded is
    raised before any request), accounted with its real usage right after the response (before any
    parsing, so rejected answers count), and charged in full if it fails without usage."""
    obs = _observability()
    call = obs.before_call(provider=provider, model=model, base_url=base_url,
                           prompt_tokens_upper_bound=prompt_bound,
                           max_completion_tokens=max_completion_tokens) if obs else None
    try:
        response = attempt()
    except BaseException:
        if call is not None:
            obs.after_call(call, failed=True)
        raise
    prompt, completion = _usage_counts(response, provider)
    _record_usage(provider, model, prompt, completion)
    if call is not None:
        obs.after_call(call, prompt_tokens=prompt, completion_tokens=completion)
    return response


def _metered_method(create, *, provider: str, model: str, base_url: str | None, default_max_tokens: int):
    """A provider client's create(...) with every call metered: Instructor calls it once per attempt,
    retries included, with the repair conversation in `messages`."""

    def run(*args, **kwargs):
        bound = prompt_token_bound(kwargs.get("system"), kwargs.get("messages"), kwargs.get("tools"),
                                   kwargs.get("tool_choice"), kwargs.get("response_format"), kwargs.get("functions"))
        limit = kwargs.get("max_tokens") or default_max_tokens
        kwargs["max_tokens"] = limit
        return metered_attempt(provider=provider, model=model, base_url=base_url, prompt_bound=bound,
                               max_completion_tokens=limit, attempt=lambda: create(*args, **kwargs))

    return run


def _log_usage(model: str, response, provider: str = "openai") -> None:
    """Tokens per call (every attempt, every provider), for cost and load: grep "LLM usage" in the
    logs. OpenAI names them prompt/completion tokens, Anthropic input/output."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
    completion = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
    _record_usage(provider, model, prompt, completion)


def strip_json_fences(payload: str) -> str:
    """Hosted models often wrap JSON in ```json fences even when asked not to."""
    candidate = payload.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    return candidate


class AnthropicCompletionClient:
    def __init__(self) -> None:
        try:
            from anthropic import Anthropic
        except ModuleNotFoundError as exc:
            raise LLMError("Anthropic provider requires the 'anthropic' package: pip install anthropic") from exc
        self._client = Anthropic(max_retries=0)  # each retry would be an unmetered billable attempt

    def complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_ms: int,
                      max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        response = metered_attempt(
            provider="anthropic", model=model, base_url=None,
            prompt_bound=prompt_token_bound(system_prompt, user_prompt), max_completion_tokens=max_tokens,
            attempt=lambda: self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                timeout=timeout_ms / 1000,
            ))
        return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


class OpenAICompletionClient:
    """OpenAI, or any OpenAI-compatible server (Ollama, vLLM) through OPENAI_BASE_URL."""

    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise LLMError("OpenAI provider requires the 'openai' package: pip install openai") from exc
        self._client = OpenAI(max_retries=0)  # each retry would be an unmetered billable attempt

    def complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_ms: int,
                      max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        # The output limit is sent, so the spend reservation's completion bound holds.
        response = metered_attempt(
            provider="openai", model=model, base_url=str(self._client.base_url),
            prompt_bound=prompt_token_bound(messages, {"type": "json_object"}), max_completion_tokens=max_tokens,
            attempt=lambda: self._client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
                timeout=timeout_ms / 1000,
            ))
        content = response.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned an empty response")
        return content


class BedrockCompletionClient:
    def __init__(self) -> None:
        try:
            import boto3
        except ModuleNotFoundError as exc:
            raise LLMError("Bedrock provider requires boto3 to be installed in the backend environment") from exc
        from botocore.config import Config

        # SDK retries off: each would be an unmetered billable attempt.
        self._client = boto3.client("bedrock-runtime", config=Config(retries={"max_attempts": 1, "mode": "standard"}))

    def complete_json(self, *, system_prompt: str, user_prompt: str, model: str, timeout_ms: int,
                      max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        del timeout_ms
        response = metered_attempt(
            provider="bedrock", model=model, base_url=None,
            prompt_bound=prompt_token_bound(system_prompt, user_prompt), max_completion_tokens=max_tokens,
            attempt=lambda: self._client.converse(
                modelId=model,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                inferenceConfig={"temperature": 0, "maxTokens": max_tokens},
            ))
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block.get("text", "") for block in blocks if "text" in block)
        if not text:
            raise LLMError("Bedrock returned an empty response")
        return text


_KEY_VARS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def missing_api_key(provider: Provider | None) -> str | None:
    """The environment variable a provider needs but lacks, or None. Checked for the provider a
    task actually resolves to (review B14: schema generation checked the base provider's key)."""
    import os

    var = _KEY_VARS.get(provider or "")
    return var if var and not os.environ.get(var) else None


def build_completion_client(provider: Provider | None) -> LLMCompletionClient:
    if provider == "anthropic":
        return AnthropicCompletionClient()
    if provider == "openai":
        return OpenAICompletionClient()
    if provider == "bedrock":
        return BedrockCompletionClient()
    raise LLMError("LLM_PROVIDER must be set to anthropic, openai or bedrock")


def _answer_in_content(exc: Exception, response_model: type[T]) -> T | None:
    """The answer the model wrote as message text instead of a tool call. qwen3:4b over Ollama does
    this on long prompts (6.3k of 8k tokens once the registry listed column filters): Instructor
    then fails with "does not support multiple tool calls" although the text is a valid object.
    Parsed and validated like any other answer; anything else keeps the original error."""
    completion = getattr(exc, "last_completion", None)
    try:
        message = completion.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return None
    if getattr(message, "tool_calls", None) or not isinstance(getattr(message, "content", None), str):
        return None
    text = message.content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
        if isinstance(payload, dict) and "intent" in payload and isinstance(payload["intent"], dict):
            payload = payload["intent"]  # the examples' {"question", "intent"} shape
        result = response_model.model_validate(payload)
    except Exception:  # not the object we asked for: the original error stands
        return None
    logger.info("Structured output recovered from message text (no tool call)")
    return result


def structured_completion(
    *,
    provider: Provider | None,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    timeout_ms: int,
    max_tokens: int | None = None,
    max_retries: int | None = None,
) -> T:
    """A Pydantic object from the LLM via Instructor (tool calling). Bedrock is not supported here;
    callers fall back to `build_completion_client(...).complete_json`."""
    try:
        import instructor
    except ModuleNotFoundError as exc:
        raise LLMError("Instructor is required for structured LLM output") from exc
    extra = {} if max_retries is None else {"max_retries": max_retries}
    if provider == "openai":
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise LLMError("OpenAI provider requires the 'openai' package") from exc
        extra["max_tokens"] = max_tokens or DEFAULT_MAX_TOKENS  # always sent: it bounds the reservation
        raw = OpenAI(max_retries=0)
        # Every attempt metered, Instructor's retries included: it calls this create once per attempt.
        raw.chat.completions.create = _metered_method(raw.chat.completions.create, provider="openai", model=model,
                                                      base_url=str(raw.base_url), default_max_tokens=extra["max_tokens"])
        client = instructor.from_openai(raw)
        try:
            result, _completion = client.chat.completions.create_with_completion(
                model=model, temperature=0, response_model=response_model,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                timeout=timeout_ms / 1000, **extra,
            )
        except Exception as exc:
            if (refusal := _budget_refusal(exc)) is not None:
                raise refusal from None
            recovered = _answer_in_content(exc, response_model)
            if recovered is None:
                raise
            return recovered
        return result
    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ModuleNotFoundError as exc:
            raise LLMError("Anthropic provider requires the 'anthropic' package") from exc
        raw = Anthropic(max_retries=0)
        raw.messages.create = _metered_method(raw.messages.create, provider="anthropic", model=model, base_url=None,
                                              default_max_tokens=max_tokens or DEFAULT_MAX_TOKENS)
        client = instructor.from_anthropic(raw)
        try:
            return client.messages.create(
                model=model, max_tokens=max_tokens or DEFAULT_MAX_TOKENS, temperature=0, system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}], response_model=response_model,
                timeout=timeout_ms / 1000, **extra,
            )
        except Exception as exc:
            if (refusal := _budget_refusal(exc)) is not None:
                raise refusal from None
            raise
    raise LLMError(f"Structured output is not configured for provider {provider!r}")
