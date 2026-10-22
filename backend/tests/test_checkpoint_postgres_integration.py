"""Conversation state in Postgres survives a restart (skipped unless NLVIZ_TEST_DATABASE_URL is set)."""
from __future__ import annotations

import os
import uuid

import pytest

from app.agent.checkpoint import build_checkpointer
from app.agent.graph import QueryGraphDependencies, QueryGraphRunner
from app.services import IntentMapperRouter

from test_agent_conversation import LLM_CONFIG, FakeIndex, ScriptedLLM, _intent, _state_filter
from test_query_graph import StubConnector, _load_registry

DATABASE_URL = os.getenv("NLVIZ_TEST_DATABASE_URL")
if not DATABASE_URL:
    pytest.skip("NLVIZ_TEST_DATABASE_URL not set; Postgres checkpointer tests skipped", allow_module_level=True)


def _runner(checkpointer, llm):
    connector = StubConnector()
    return QueryGraphRunner(
        QueryGraphDependencies(
            connector=connector,
            schema_context=connector.get_schema(),
            registry=_load_registry(),
            intent_mapper=IntentMapperRouter(config=LLM_CONFIG, llm_mapper=llm, value_index=lambda: FakeIndex()),
            intent_config=LLM_CONFIG,
        ),
        checkpointer=checkpointer.saver,
    ), connector


def test_paused_conversation_and_its_memory_survive_a_restart():
    thread = f"test-user/default/{uuid.uuid4().hex}"
    first_process = build_checkpointer(DATABASE_URL)
    assert first_process.durable
    try:
        runner, _ = _runner(first_process, ScriptedLLM(_intent(filters=_state_filter(customer_state="Rio"))))
        paused = runner.invoke(question="revenue in Rio", query_id="q1", thread_id=thread)
        assert paused["clarification"]["options"] == ["RJ", "RN", "RS"]
    finally:
        first_process.close()

    second_process = build_checkpointer(DATABASE_URL)  # setup() again is a no-op
    try:
        follow_up = _intent(filters=_state_filter(customer_state="RJ", payment_type="voucher"))
        llm = ScriptedLLM(follow_up)
        runner, connector = _runner(second_process, llm)
        assert runner.pending_clarification(thread)["raw"] == "Rio"

        done = runner.resume(thread_id=thread, answer="RJ")
        assert done["intent"].filters[0].value == "RJ"
        assert "'RJ'" in connector.executed_sql[-1]

        nxt = runner.invoke(question="only vouchers", query_id="q2", thread_id=thread)
        assert "Previous question in this conversation: revenue in Rio" in llm.calls[-1]["context"]
        assert nxt["user_trace"].count("Summary generated") == 1
        assert "voucher" in connector.executed_sql[-1]
    finally:
        runner.forget(thread)
        assert runner.pending_clarification(thread) is None
        second_process.close()
