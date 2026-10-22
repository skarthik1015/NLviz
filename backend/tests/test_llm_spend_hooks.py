"""The spending cap at every billable provider attempt (app.llm; contract in app/observability/README.md):
a blocked hosted call never reaches the provider, each Instructor retry is its own metered attempt,
local models are not capped, and no code path catches the refusal to try another hosted call."""
from __future__ import annotations

from decimal import Decimal

import pytest
from openai.resources.chat.completions import Completions
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from app.observability import Observability, configure
from app.observability.budget import BudgetExceeded, SpendStore

HOSTED = "https://api.openai.com/v1"


class Answer(BaseModel):
    value: int


def _completion(arguments: str | None = None, content: str | None = None) -> ChatCompletion:
    message = {"role": "assistant", "content": content}
    if arguments is not None:
        message["tool_calls"] = [{"id": "t1", "type": "function", "function": {"name": "Answer", "arguments": arguments}}]
    return ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-4.1-mini",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


@pytest.fixture()
def provider(monkeypatch):
    """The OpenAI SDK's create, answering from a script and counting real requests."""
    sent: list[dict] = []
    script: list[ChatCompletion] = []

    def create(self, *args, **kwargs):
        sent.append(kwargs)
        return script.pop(0)

    monkeypatch.setattr(Completions, "create", create)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", HOSTED)
    return sent, script


@pytest.fixture()
def ledger(tmp_path):
    services = []

    def install(cap):
        service = Observability(SpendStore(tmp_path / "spend.sqlite3"), cap=cap)
        configure(service)
        services.append(service)
        return service

    yield install
    configure(None)


def test_a_blocked_hosted_call_never_reaches_the_provider(provider, ledger):
    from app.llm import OpenAICompletionClient

    sent, script = provider
    ledger(Decimal("0"))
    with pytest.raises(BudgetExceeded):
        OpenAICompletionClient().complete_json(system_prompt="s", user_prompt="u", model="gpt-4.1-mini", timeout_ms=1000)
    assert sent == []


def test_an_admitted_call_is_charged_its_real_usage_and_sends_the_output_limit(provider, ledger):
    from app.llm import OpenAICompletionClient, usage_meter

    sent, script = provider
    script.append(_completion(content='{"ok": true}'))
    service = ledger(Decimal("5"))
    with usage_meter() as calls:
        OpenAICompletionClient().complete_json(system_prompt="s", user_prompt="u", model="gpt-4.1-mini",
                                               timeout_ms=1000, max_tokens=300)
    assert sent[0]["max_tokens"] == 300
    snapshot = service.store.snapshot()
    assert Decimal(snapshot["spent_usd"]) == Decimal("0.000072")  # 100 x $0.40/M + 20 x $1.60/M
    assert Decimal(snapshot["reserved_usd"]) == 0
    assert len(calls) == 1  # the audit meter sees it once


def test_each_instructor_retry_is_a_separate_metered_attempt(provider, ledger):
    from app.llm import structured_completion

    sent, script = provider
    script += [_completion(arguments='{"value": "not a number"}'), _completion(arguments='{"value": 7}')]
    service = ledger(Decimal("5"))
    result = structured_completion(provider="openai", model="gpt-4.1-mini", system_prompt="s", user_prompt="u",
                                   response_model=Answer, timeout_ms=1000, max_retries=2)
    assert result.value == 7 and len(sent) == 2
    assert Decimal(service.store.snapshot()["spent_usd"]) == Decimal("0.000144")  # both attempts charged
    assert all(request["max_tokens"] for request in sent)


def test_a_blocked_structured_call_makes_no_request(provider, ledger):
    from app.llm import structured_completion

    sent, _ = provider
    ledger(Decimal("0"))
    with pytest.raises(BudgetExceeded):
        structured_completion(provider="openai", model="gpt-4.1-mini", system_prompt="s", user_prompt="u",
                              response_model=Answer, timeout_ms=1000)
    assert sent == []


def test_local_models_are_not_capped(provider, ledger, monkeypatch):
    from app.llm import OpenAICompletionClient

    sent, script = provider
    monkeypatch.setenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
    script.append(_completion(content="{}"))
    ledger(Decimal("0"))
    OpenAICompletionClient().complete_json(system_prompt="s", user_prompt="u", model="qwen3:4b", timeout_ms=1000)
    assert len(sent) == 1


def test_a_failed_attempt_without_usage_is_charged_in_full(provider, ledger, monkeypatch):
    from app.llm import OpenAICompletionClient

    def broken(self, *args, **kwargs):
        raise ConnectionError("reset")

    monkeypatch.setattr(Completions, "create", broken)
    service = ledger(Decimal("5"))
    with pytest.raises(ConnectionError):
        OpenAICompletionClient().complete_json(system_prompt="s", user_prompt="u", model="gpt-4.1-mini", timeout_ms=1000)
    snapshot = service.store.snapshot()
    assert Decimal(snapshot["spent_usd"]) > 0 and Decimal(snapshot["reserved_usd"]) == 0


class _BlockedClient:
    def complete_json(self, **_):
        raise BudgetExceeded("cap")


def test_no_path_catches_the_refusal_to_call_the_model_again(monkeypatch):
    import app.services.intent_mapper as mapper_module
    import app.services.schema_generator as generator
    from app.services.intent_mapper import IntentMapperConfig, LLMIntentMapper, check_answerable

    from tests.test_query_graph import StubConnector, _load_registry

    registry = _load_registry()
    # The answerability check runs beside the mapping: it must not turn the refusal into "answerable".
    with pytest.raises(BudgetExceeded):
        check_answerable("q", registry, _BlockedClient(), "m", 1)
    # The mapper's JSON fallback is another hosted call.
    json_fallback = []
    monkeypatch.setattr(mapper_module, "_extract_intent_with_instructor", lambda **_: (_ for _ in ()).throw(BudgetExceeded("cap")))
    monkeypatch.setattr(mapper_module, "_extract_intent_from_json_client", lambda **_: json_fallback.append(1))
    config = IntentMapperConfig(mode="llm", provider="openai", model="m")
    with pytest.raises(BudgetExceeded):
        LLMIntentMapper(config=config, client=_BlockedClient()).map("q", registry, StubConnector().get_schema())
    assert json_fallback == []
    # Schema generation's JSON fallback likewise.
    monkeypatch.setattr(generator, "_generate_with_instructor", lambda *_: (_ for _ in ()).throw(BudgetExceeded("cap")))
    monkeypatch.setattr(generator, "_generate_with_json_client", lambda *_: json_fallback.append(2))
    with pytest.raises(BudgetExceeded):
        generator._generate_via_llm("summary", config)
    assert json_fallback == []


def test_the_api_says_the_budget_is_spent():
    from app.api.routes.chat import _error

    assert _error(BudgetExceeded("cap")) == (503, "LLM_BUDGET_EXCEEDED")


def test_anthropic_and_bedrock_calls_are_blocked_before_any_request(ledger, monkeypatch):
    from anthropic.resources.messages import Messages

    from app.llm import AnthropicCompletionClient, BedrockCompletionClient, structured_completion

    sent = []
    monkeypatch.setattr(Messages, "create", lambda self, *a, **k: sent.append(k))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    ledger(Decimal("0"))
    monkeypatch.setenv("LLM_PRICE_TABLE_JSON", "")
    for call in (
        lambda: AnthropicCompletionClient().complete_json(system_prompt="s", user_prompt="u",
                                                          model="claude-haiku-5-5", timeout_ms=1000),
        lambda: structured_completion(provider="anthropic", model="claude-haiku-5-5", system_prompt="s",
                                      user_prompt="u", response_model=Answer, timeout_ms=1000),
    ):
        # No price is configured for this model: with a cap set, an unpriced hosted call fails closed.
        with pytest.raises(BudgetExceeded):
            call()
    client = BedrockCompletionClient()
    monkeypatch.setattr(client._client, "converse", lambda **k: sent.append(k))
    with pytest.raises(BudgetExceeded):
        client.complete_json(system_prompt="s", user_prompt="u", model="anthropic.claude", timeout_ms=1000)
    assert sent == []
