"""Phase 3 agent behaviour with a scripted LLM: follow-ups, clarification, repair."""
from __future__ import annotations

import pandas as pd
import pytest

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.agent.graph import NoPendingClarificationError
from app.agent.nodes.intent_mapper import ANY_VALUE
from app.agent.state import MAX_REPAIRS
from app.grounding.values import SnapResult, ValueMatch
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.security import SQLSafetyError
from app.services import IntentMapperConfig, IntentMapperRouter

from test_query_graph import StubConnector, _load_registry  # tests/ is on sys.path under pytest

REGISTRY = _load_registry()
LLM_CONFIG = IntentMapperConfig(mode="llm", provider=None, model="scripted")


class ScriptedLLM:
    """Returns the scripted intents in order (the last one repeats) and records each call."""

    def __init__(self, *intents):
        self.intents = list(intents)
        self.calls: list[dict] = []

    def map(self, question, registry, schema, value_hints=None, context=None):
        self.calls.append({"question": question, "context": context, "value_hints": value_hints})
        return self.intents[min(len(self.calls), len(self.intents)) - 1]


class FakeIndex:
    """Knows customer_state; 'Rio' is ambiguous, 'São Paulo' is a synonym of SP."""

    dimensions = {"customer_state"}

    def snap(self, dimension, raw):
        if raw in ("SP", "São Paulo"):
            return SnapResult(value="SP", confidence="exact")
        if raw in ("RS", "RN"):
            return SnapResult(value=raw, confidence="exact")
        if raw == "Rio":
            return SnapResult(value=None, confidence="weak", alternatives=["RJ", "RN", "RS"])
        return SnapResult(value=None, confidence="none")

    def candidates(self, question):
        return [ValueMatch("customer_state", "RJ", 0.5)] if "Rio" in question else []

    def assumption(self, question, dimension, value):
        """RS and RN are supported only by their full names; everything else is supported."""
        if value in ("RS", "RN") and "do Sul" not in question and "do Norte" not in question:
            return [value, "RN" if value == "RS" else "RS"]
        return None


class FailingOnceConnector(StubConnector):
    def __init__(self, error: Exception):
        super().__init__()
        self.error = error

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        if self.error is not None:
            error, self.error = self.error, None
            self.executed_sql.append(sql)
            raise error
        return super().execute_query(sql, limit)


def _runner(llm, *, connector=None, index=None, config=LLM_CONFIG):
    connector = connector or StubConnector()
    return QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=connector.get_schema(),
            registry=REGISTRY,
            intent_mapper=IntentMapperRouter(config=config, llm_mapper=llm, value_index=lambda: index),
            intent_config=config,
        )
    ), connector


def _intent(**kwargs):
    return SemanticIntent(**{"metric": "total_revenue", "limit": 25, **kwargs})


def _state_filter(**filters):
    return [FilterCondition(dimension=d, operator="eq", value=v) for d, v in filters.items()]


# Follow-ups ------------------------------------------------------------------------------------


def test_follow_up_sees_the_previous_turn_and_traces_are_per_turn():
    first = _intent(dimensions=["customer_state"])
    second = _intent(dimensions=["customer_state"], filters=_state_filter(payment_type="credit_card"))
    llm = ScriptedLLM(first, second)
    runner, connector = _runner(llm)

    one = runner.invoke(question="revenue by state", query_id="q1", thread_id="t")
    two = runner.invoke(question="only credit card payments", query_id="q2", thread_id="t")

    assert llm.calls[0]["context"] is None
    assert "Previous question in this conversation: revenue by state" in llm.calls[1]["context"]
    assert '"dimensions":["customer_state"]' in llm.calls[1]["context"]
    assert two["intent"].filters[0].value == "credit_card"
    assert "credit_card" in connector.executed_sql[-1]
    assert two["previous_turn"]["question"] == "only credit card payments"
    # The second turn's trace starts fresh rather than appending to the first.
    assert two["user_trace"][0].startswith("Understood:")
    assert two["user_trace"].count("Summary generated") == 1
    assert one["user_trace"].count("Summary generated") == 1


def test_one_off_questions_share_nothing_and_leave_no_thread():
    llm = ScriptedLLM(_intent(dimensions=["customer_state"]))
    runner, _ = _runner(llm)

    runner.invoke(question="revenue by state", query_id="q1")
    runner.invoke(question="revenue by state again", query_id="q2")

    assert [call["context"] for call in llm.calls] == [None, None]
    assert runner._graph.get_state({"configurable": {"thread_id": "single:q1"}}).values == {}


# Clarification ---------------------------------------------------------------------------------


def test_ambiguous_value_pauses_then_resumes_with_the_chosen_value():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="Rio")))
    runner, connector = _runner(llm, index=FakeIndex())

    paused = runner.invoke(question="revenue in Rio", query_id="q1", thread_id="t")

    assert paused["clarification"]["options"] == ["RJ", "RN", "RS"]
    assert paused["clarification"]["dimension"] == "customer_state"
    assert paused["thread_id"] == "t"
    assert connector.executed_sql == []  # nothing ran while waiting
    assert runner.pending_clarification("t")["raw"] == "Rio"

    done = runner.resume(thread_id="t", answer="RJ")

    assert "clarification" not in done
    assert done["intent"].filters[0].value == "RJ"
    assert "'RJ'" in connector.executed_sql[-1]
    assert {"kind": "clarification", "dimension": "customer_state", "text": "Rio", "value": "RJ"} in done["evidence"]
    assert runner.pending_clarification("t") is None


def test_answering_any_value_drops_the_filter():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="Rio")))
    runner, connector = _runner(llm, index=FakeIndex())
    runner.invoke(question="revenue in Rio", query_id="q1", thread_id="t")

    done = runner.resume(thread_id="t", answer=ANY_VALUE)

    assert done["intent"].filters == []
    assert "WHERE" not in connector.executed_sql[-1] or "customer_state" not in connector.executed_sql[-1]


def test_confident_values_are_snapped_without_asking():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="São Paulo")))
    runner, connector = _runner(llm, index=FakeIndex())

    done = runner.invoke(question="revenue in São Paulo", query_id="q1")

    assert "clarification" not in done
    assert done["intent"].filters[0].value == "SP"
    assert any(e["kind"] == "match" and e["value"] == "SP" for e in done["evidence"])


def test_one_off_question_can_be_clarified_and_is_then_forgotten():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="Rio")))
    runner, _ = _runner(llm, index=FakeIndex())

    paused = runner.invoke(question="revenue in Rio", query_id="q1")
    done = runner.resume(thread_id=paused["thread_id"], answer="RS")

    assert done["intent"].filters[0].value == "RS"
    assert runner._graph.get_state({"configurable": {"thread_id": paused["thread_id"]}}).values == {}


def test_resume_without_a_pending_question_is_refused():
    runner, _ = _runner(ScriptedLLM(_intent()))
    runner.invoke(question="total revenue", query_id="q1", thread_id="t")

    with pytest.raises(NoPendingClarificationError):
        runner.resume(thread_id="t", answer="RJ")
    with pytest.raises(NoPendingClarificationError):
        runner.resume(thread_id="never-used", answer="RJ")


# Repair ----------------------------------------------------------------------------------------


def test_invalid_llm_intent_is_repaired_by_the_llm_before_any_fallback():
    llm = ScriptedLLM(_intent(dimensions=["no_such_dimension"]), _intent(dimensions=["customer_state"]))
    runner, _ = _runner(llm)

    done = runner.invoke(question="revenue by state", query_id="q1")

    assert done["intent_source"] == "llm"
    assert done["intent"].dimensions == ["customer_state"]
    assert len(llm.calls) == 2
    assert "rejected" in llm.calls[1]["context"] and "no_such_dimension" in llm.calls[1]["context"]
    assert done["repair_count"] == 1


def test_repairs_are_bounded_then_fall_back_to_the_heuristic_mapper():
    llm = ScriptedLLM(_intent(dimensions=["no_such_dimension"]))
    runner, _ = _runner(llm)

    done = runner.invoke(question="show revenue by state", query_id="q1")

    assert len(llm.calls) == 1 + MAX_REPAIRS
    assert done["intent_source"] == "llm_fallback"
    assert done["intent"].metric == "total_revenue"
    # The answer says it is a simpler reading, and what it computed (not a silent partial answer).
    assert "simpler reading: Total Revenue" in done["explanation"]


def test_engine_error_is_repaired_by_reinterpreting():
    llm = ScriptedLLM(_intent(dimensions=["customer_state"]), _intent(dimensions=["payment_type"]))
    runner, connector = _runner(llm, connector=FailingOnceConnector(RuntimeError("Schema error: no field x\ndetail")))

    done = runner.invoke(question="revenue by state", query_id="q1")

    assert len(connector.executed_sql) == 2
    assert "failed on the engine: Schema error: no field x." in llm.calls[1]["context"]
    assert done["intent"].dimensions == ["payment_type"]
    assert done["row_count"] == 2


def test_refusals_and_non_llm_intents_are_not_repaired():
    runner, _ = _runner(ScriptedLLM(_intent()), connector=FailingOnceConnector(SQLSafetyError("outside allowlist")))
    with pytest.raises(SQLSafetyError):
        runner.invoke(question="total revenue", query_id="q1")

    heuristic = IntentMapperConfig(mode="heuristic")
    runner, _ = _runner(None, connector=FailingOnceConnector(RuntimeError("boom")), config=heuristic)
    with pytest.raises(RuntimeError, match="boom"):
        runner.invoke(question="show revenue by state", query_id="q2")


def test_a_value_the_question_does_not_name_is_confirmed_with_the_user():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="RS")))
    runner, connector = _runner(llm, index=FakeIndex())

    paused = runner.invoke(question="revenue in Rio Grande", query_id="q1", thread_id="t")

    assert paused["clarification"]["reason"] == "assumed"
    assert paused["clarification"]["options"] == ["RS", "RN"]
    assert "I assumed RS" in paused["clarification"]["prompt"]
    assert connector.executed_sql == []

    done = runner.resume(thread_id="t", answer="RN")  # the user's choice is not questioned again
    assert "clarification" not in done
    assert done["intent"].filters[0].value == "RN"

    # A follow-up keeps the confirmed filter without asking again.
    llm.intents = [_intent(dimensions=["payment_type"], filters=_state_filter(customer_state="RN"))]
    follow_up = runner.invoke(question="now by payment type", query_id="q2", thread_id="t")
    assert "clarification" not in follow_up
    assert "'RN'" in connector.executed_sql[-1]


def test_confirming_the_assumed_value_keeps_it():
    llm = ScriptedLLM(_intent(filters=_state_filter(customer_state="RS")))
    runner, _ = _runner(llm, index=FakeIndex())
    runner.invoke(question="revenue in Rio Grande", query_id="q1", thread_id="t")

    done = runner.resume(thread_id="t", answer="RS")

    assert done["intent"].filters[0].value == "RS"
    assert "clarification" not in done


# A breakdown filtered to one of its own values ------------------------------------------------


def test_breakdown_filtered_to_one_value_keeps_what_the_question_names():
    grouped_and_filtered = _intent(dimensions=["customer_state"], filters=_state_filter(customer_state="SP"))

    runner, _ = _runner(ScriptedLLM(grouped_and_filtered), index=FakeIndex())
    named = runner.invoke(question="revenue from São Paulo customers", query_id="q1")
    assert named["intent"].dimensions == [] and named["intent"].filters[0].value == "SP"

    runner, _ = _runner(ScriptedLLM(grouped_and_filtered), index=FakeIndex())
    by_state = runner.invoke(question="revenue by customer state", query_id="q2")
    assert by_state["intent"].dimensions == ["customer_state"] and by_state["intent"].filters == []
    assert any("dropped filter" in line for line in by_state["debug_trace"])


def test_breakdown_rule_leaves_carried_filters_and_paraphrased_values():
    status = SemanticIntent(metric="cancellation_rate", dimensions=["order_status"],
                            filters=[FilterCondition(dimension="order_status", operator="eq", value="canceled")])
    runner, _ = _runner(ScriptedLLM(status))
    state = runner.invoke(question="Show cancellation rate by order status", query_id="q3")
    assert state["intent"].filters == [] and state["intent"].dimensions == ["order_status"]

    first = _intent(filters=_state_filter(customer_state="SP"))
    follow = _intent(dimensions=["customer_state"], filters=_state_filter(customer_state="SP"))
    runner, _ = _runner(ScriptedLLM(first, follow), index=FakeIndex())
    runner.invoke(question="revenue in São Paulo", query_id="q4", thread_id="t-carry")
    state = runner.invoke(question="now by state", query_id="q5", thread_id="t-carry")
    assert state["intent"].dimensions == ["customer_state"] and state["intent"].filters[0].value == "SP"


def test_asks_breakdown_matches_label_name_and_last_word():
    from app.agent.nodes.intent_mapper import _asks_breakdown

    state = REGISTRY.get_dimension("customer_state")
    assert _asks_breakdown("Revenue by customer state", state)
    assert _asks_breakdown("orders per state", state)
    assert _asks_breakdown("revenue for each of the states", state) is False  # "each of the" is not "each state"
    assert _asks_breakdown("revenue across states", state)
    assert not _asks_breakdown("revenue from customers in Bahia", state)
    category = REGISTRY.get_dimension("product_category")
    assert not _asks_breakdown("Average review score for health and beauty products", category)


# Questions the data cannot answer ---------------------------------------------------------------


def test_unsupported_question_ends_without_a_query_and_says_what_is_missing():
    llm = ScriptedLLM(_intent(dimensions=["product_category"], unsupported="product weight"))
    runner, connector = _runner(llm)
    state = runner.invoke(question="average product weight by category", query_id="u1")
    assert connector.executed_sql == []
    assert state["unsupported"] == "product weight" and state["sql"] is None
    assert state["explanation"].startswith("This data has no product weight")
    assert "Total Revenue" in state["explanation"] and "Customer State" in state["explanation"]

    from app.services.query_service import QueryService

    llm = ScriptedLLM(_intent(unsupported="not a data question"))
    response = QueryService(_runner(llm)[0]).run_question("write me a poem")
    assert response.status == "unsupported" and response.sql is None and response.rows == []


def test_a_follow_up_after_an_unsupported_turn_still_sees_the_last_answer():
    answered = _intent(dimensions=["customer_state"])
    llm = ScriptedLLM(answered, _intent(unsupported="weather"), _intent(dimensions=["payment_type"]))
    runner, _ = _runner(llm)
    runner.invoke(question="revenue by state", query_id="a", thread_id="t-u")
    runner.invoke(question="and the weather?", query_id="b", thread_id="t-u")
    runner.invoke(question="now by payment type", query_id="c", thread_id="t-u")
    assert "revenue by state" in llm.calls[2]["context"]


def test_answerability_check_marks_the_intent_and_is_skipped_on_repairs():
    asked = []

    def check(question, registry, previous=None):
        asked.append(question)
        return "product weight" if "weight" in question else None

    llm = ScriptedLLM(_intent(dimensions=["product_category"]))
    router = IntentMapperRouter(config=LLM_CONFIG, llm_mapper=llm, answerability=check)
    connector = StubConnector()
    runner = QueryGraphRunner(QueryGraphDependencies(connector=connector, schema_context=connector.get_schema(),
                                                     registry=REGISTRY, intent_mapper=router, intent_config=LLM_CONFIG))
    state = runner.invoke(question="average product weight by category", query_id="w")
    assert state["unsupported"] == "product weight" and connector.executed_sql == []

    bad_then_good = ScriptedLLM(_intent(dimensions=["no_such_dimension"]), _intent(dimensions=["customer_state"]))
    router = IntentMapperRouter(config=LLM_CONFIG, llm_mapper=bad_then_good, answerability=check)
    runner = QueryGraphRunner(QueryGraphDependencies(connector=connector, schema_context=connector.get_schema(),
                                                     registry=REGISTRY, intent_mapper=router, intent_config=LLM_CONFIG))
    asked.clear()
    state = runner.invoke(question="revenue by state", query_id="r")
    assert state["intent"].dimensions == ["customer_state"] and asked == ["revenue by state"]  # once, not per repair



def test_answerability_check_sees_the_previous_question_for_follow_ups():
    seen = []

    def check(question, registry, previous=None):
        seen.append((question, previous))
        return None

    llm = ScriptedLLM(_intent(time_dimension="order_date", time_granularity="month", order_by="time_asc"))
    router = IntentMapperRouter(config=LLM_CONFIG, llm_mapper=llm, answerability=check)
    connector = StubConnector()
    runner = QueryGraphRunner(QueryGraphDependencies(connector=connector, schema_context=connector.get_schema(),
                                                     registry=REGISTRY, intent_mapper=router, intent_config=LLM_CONFIG))
    runner.invoke(question="Monthly revenue", query_id="1", thread_id="t-ans")
    runner.invoke(question="Make it quarterly", query_id="2", thread_id="t-ans")
    assert seen == [("Monthly revenue", None), ("Make it quarterly", "Monthly revenue")]


def test_a_refusal_is_ignored_for_a_follow_up_that_keeps_the_metric():
    def check(question, registry, previous=None):
        return "worst 5 what" if question == "Show the worst 5" else ("customer city" if "city" in question else None)

    llm = ScriptedLLM(_intent(dimensions=["customer_state"]),
                      _intent(dimensions=["customer_state"], order_by="metric_asc", limit=5),
                      SemanticIntent(metric="order_count", dimensions=["customer_state"]))
    router = IntentMapperRouter(config=LLM_CONFIG, llm_mapper=llm, answerability=check)
    connector = StubConnector()
    runner = QueryGraphRunner(QueryGraphDependencies(connector=connector, schema_context=connector.get_schema(),
                                                     registry=REGISTRY, intent_mapper=router, intent_config=LLM_CONFIG))
    runner.invoke(question="revenue by state", query_id="1", thread_id="t-keep")
    state = runner.invoke(question="Show the worst 5", query_id="2", thread_id="t-keep")
    assert not state.get("unsupported") and state["intent"].limit == 5
    state = runner.invoke(question="orders by customer city", query_id="3", thread_id="t-keep")
    assert state["unsupported"] == "customer city"  # a new metric: the check applies


class _AverageIndex(FakeIndex):
    """Snaps the LLM's 'avg rating' to the order status 'Average' (a fuzzy match)."""

    dimensions = {"customer_state", "order_status"}

    def snap(self, dimension, raw):
        if dimension == "order_status" and raw == "avg rating":
            return SnapResult(value="Average", confidence="fuzzy")
        return super().snap(dimension, raw)

    def assumption(self, question, dimension, value):
        return None


def test_an_aggregation_word_that_snapping_produced_is_dropped_too():
    llm = ScriptedLLM(SemanticIntent(metric="average_review_score", filters=[
        FilterCondition(dimension="order_status", operator="eq", value="avg rating")]))
    runner, _ = _runner(llm, index=_AverageIndex())

    done = runner.invoke(question="Average review score by customer state", query_id="q1")

    assert done["intent"].filters == []
