"""The audit trail (what happened to the intent, as data) and the switch for the phrase rules."""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.agent.nodes.intent_mapper import PHRASE_REWRITES
from app.llm import _log_usage, usage_meter
from app.models import SemanticIntent
from app.services import IntentMapperConfig, IntentMapperRouter, QueryService

from tests.test_query_graph import StubConnector, _load_registry

QUESTION = "How many orders came from SP?"


class _LLM:
    """An LLM mapper that answers with a sum where the question asks for a count, and reports tokens."""

    def map(self, question, registry, schema, **kwargs):
        del question, registry, schema, kwargs
        _log_usage("stub-model", SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30)))
        return SemanticIntent(metric="total_revenue")


def _service(phrase_rewrites: bool) -> QueryService:
    connector = StubConnector()
    config = IntentMapperConfig(mode="llm", provider="openai", model="stub-model", phrase_rewrites=phrase_rewrites)
    return QueryService(QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=_load_registry(),
        intent_mapper=IntentMapperRouter(config=config, llm_mapper=_LLM()), intent_config=config)))


def test_each_rule_rewrite_is_recorded_with_the_intent_before_and_after():
    response = _service(True).run_question(QUESTION, debug=True)
    assert response.intent.metric == "order_count"
    steps = response.audit["steps"]
    assert steps[0]["stage"] == "proposal" and steps[0]["intent"]["metric"] == "total_revenue"
    rewrite = next(s for s in steps if s["stage"] == "rewrite")
    assert rewrite["rule"] == "how_many_counts"
    assert (rewrite["before"]["metric"], rewrite["after"]["metric"]) == ("total_revenue", "order_count")
    assert steps[-1] == {"stage": "checked", "source": "llm", "intent": response.intent.model_dump(mode="json", exclude_none=True)}
    run = response.audit["run"]
    assert run["phrase_rewrites"] is True and run["model"] == "stub-model" and len(run["schema"]["fingerprint"]) == 12


def test_phrase_rewrites_off_leaves_the_llm_intent_alone():
    response = _service(False).run_question(QUESTION, debug=True)
    assert response.intent.metric == "total_revenue"
    assert not [s for s in response.audit["steps"] if s["stage"] == "rewrite"]
    assert response.audit["run"]["phrase_rewrites"] is False


def test_the_audit_is_only_returned_in_debug_mode():
    assert _service(True).run_question(QUESTION).audit is None


def test_tokens_are_counted_per_question_inside_the_graph():
    audit = _service(True).run_question(QUESTION, debug=True).audit
    assert audit["llm_calls"] == [{"provider": "openai", "model": "stub-model", "prompt_tokens": 120, "completion_tokens": 30}]
    assert audit["tokens"] == {"prompt": 120, "completion": 30}


def test_the_meter_sees_threads_that_copy_the_context_and_nothing_outside_it():
    usage = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1))
    _log_usage("m", usage)  # no meter open: only logged
    with usage_meter() as calls, ThreadPoolExecutor(1) as pool:
        pool.submit(contextvars.copy_context().run, _log_usage, "m", usage).result()
    assert len(calls) == 1
    _log_usage("m", usage)
    assert len(calls) == 1


def test_the_switch_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PHRASE_REWRITES", "off")
    assert IntentMapperConfig.from_env().phrase_rewrites is False
    monkeypatch.delenv("PHRASE_REWRITES")
    assert IntentMapperConfig.from_env().phrase_rewrites is True


def test_the_documented_rule_list_is_the_one_that_runs():
    from app.agent.nodes.intent_mapper import _phrase_rewrites

    assert tuple(_phrase_rewrites({"question": "x"}, _load_registry(), None, None)) == PHRASE_REWRITES


def test_replay_runs_the_recorded_proposals_with_the_rules_on_or_off():
    from app.services.replay import replay_case

    service = _service(True)
    recorded = service.run_question(QUESTION, debug=True)
    proposals = [s for s in recorded.audit["steps"] if s["stage"] == "proposal"]
    runtime = SimpleNamespace(query_graph=service.query_graph)
    on, = replay_case(runtime, [{"question": QUESTION, "proposals": proposals}], True)
    off, = replay_case(runtime, [{"question": QUESTION, "proposals": proposals}], False)
    assert on["final"]["intent"]["metric"] == recorded.intent.metric == "order_count"
    assert off["final"]["intent"]["metric"] == "total_revenue"
    assert on["final"]["audit"]["llm_calls"] == []  # the model is not asked again


def test_every_answer_carries_its_interpretation():
    response = _service(True).run_question(QUESTION)
    interpretation = response.interpretation
    assert interpretation.path == "semantic" and interpretation.metric.name == "order_count"
    assert interpretation.metric.definition.startswith("COUNT(")
    assert interpretation.policies == [] and interpretation.period is None


def test_a_period_names_the_date_it_applies_to():
    from datetime import date

    from app.services.interpretation import build_interpretation

    registry = _load_registry()
    intent = SemanticIntent(metric="order_count", dimensions=["customer_state"], start_date=date(2017, 1, 1),
                            end_date=date(2017, 12, 31))
    interpretation = build_interpretation(intent, registry, [{"kind": "note", "text": "Averaged per order."}])
    assert interpretation.period.column == registry.default_time_dimension("order_count")
    assert [g.name for g in interpretation.grouping] == ["customer_state"]
    assert interpretation.notes == ["Averaged per order."]
