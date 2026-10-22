"""Swapping models is configuration only: per-task overrides, output budgets per call, fences."""
from __future__ import annotations

import pytest

from app.llm import AnthropicCompletionClient, BedrockCompletionClient, strip_json_fences
from app.services.intent_mapper import IntentMapperConfig


def test_task_overrides_fall_back_to_the_main_settings(monkeypatch):
    base = IntentMapperConfig(mode="llm", provider="openai", model="qwen3:4b-instruct-8k")
    assert base.for_task("intent") is base
    monkeypatch.setenv("LLM_PROVIDER_SCHEMA", "anthropic")
    monkeypatch.setenv("LLM_MODEL_SCHEMA", "claude-sonnet-4-5")
    schema = base.for_task("schema")
    assert (schema.provider, schema.model, schema.timeout_ms) == ("anthropic", "claude-sonnet-4-5", base.timeout_ms)
    assert base.for_task("explain") is base
    monkeypatch.setenv("LLM_MODEL_EXPLAIN", "gpt-4.1-mini")
    assert base.for_task("explain").provider == "openai" and base.for_task("explain").model == "gpt-4.1-mini"
    monkeypatch.setenv("LLM_PROVIDER_INTENT", "nope")
    with pytest.raises(ValueError):
        base.for_task("intent")


class _Recorder:
    def __init__(self, reply):
        self.kwargs, self.reply = None, reply

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.reply


def test_max_tokens_reaches_anthropic_and_bedrock():
    """Schema generation needs ~3k output tokens; the clients used to fix 800 and truncate it."""
    anthropic = AnthropicCompletionClient.__new__(AnthropicCompletionClient)
    block = type("B", (), {"type": "text", "text": "{}"})()
    recorder = _Recorder(type("R", (), {"content": [block]})())
    anthropic._client = type("C", (), {"messages": recorder})()
    anthropic.complete_json(system_prompt="s", user_prompt="u", model="m", timeout_ms=1000, max_tokens=4096)
    assert recorder.kwargs["max_tokens"] == 4096

    bedrock = BedrockCompletionClient.__new__(BedrockCompletionClient)
    calls = {}
    bedrock._client = type("C", (), {"converse": lambda self, **kw: calls.update(kw) or
                                     {"output": {"message": {"content": [{"text": "{}"}]}}}})()
    bedrock.complete_json(system_prompt="s", user_prompt="u", model="m", timeout_ms=1000)
    assert calls["inferenceConfig"]["maxTokens"] == 800


def test_fenced_json_is_unwrapped():
    assert strip_json_fences('```json\n{"explanation": "x"}\n```') == '{"explanation": "x"}'
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'
