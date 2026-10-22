"""The guarded SQL fallback: pushdown guard, routing, and the labelled answer."""
from __future__ import annotations

import pytest

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.agent.nodes.sql_fallback import FALLBACK_REASONS
from app.models import SemanticIntent
from app.services import IntentMapperConfig, IntentMapperRouter, QueryService
from app.services.generated_sql import PushdownRefused, pushdown_guard

from tests.test_query_graph import StubConnector, _load_registry


def test_the_guard_casts_averages_and_refuses_known_pushdown_failures():
    assert "AVG(CAST(o.o_totalprice AS DOUBLE PRECISION))" in pushdown_guard(
        "SELECT o.p, AVG(o.o_totalprice) FROM orders o GROUP BY 1")
    assert "date_trunc" in pushdown_guard("SELECT date_trunc('month', o.d), COUNT(*) FROM orders o GROUP BY 1").lower()
    for sql, why in [("SELECT approx_distinct(o.c) FROM orders o", "not available"),            # J31
                     ("SELECT n, COUNT(*) FROM (SELECT DISTINCT a AS n, b FROM t) s GROUP BY n", "GROUP BY"),  # J38
                     ("SELECT my_udf(o.c) FROM orders o", "not available"),
                     ("SELECT (a FROM", "does not parse")]:
        with pytest.raises(PushdownRefused, match=why):
            pushdown_guard(sql)
    assert "COUNT(DISTINCT" in pushdown_guard("SELECT COUNT(DISTINCT o.c) FROM orders o")  # not a DISTINCT select
    assert "CAST(t.ts AS DATE)" in pushdown_guard("SELECT DATE(t.ts), COUNT(*) FROM t GROUP BY DATE(t.ts)")


def test_only_gaps_in_the_layer_fall_back_never_the_double_counting_refusals():
    assert set(FALLBACK_REASONS) == {"answerability", "not_understood"}


class _RefusingLLM:
    """The intent mapper's model: the layer has nothing for this question."""

    def map(self, question, registry, schema, **kwargs):
        del question, registry, schema, kwargs
        return SemanticIntent(metric="order_count", unsupported="number of reviews per order")


class _SqlClient:
    def __init__(self, raw):
        self.raw = raw

    def complete_json(self, **_):
        return self.raw


def _service(fallback: bool) -> QueryService:
    connector = StubConnector()
    config = IntentMapperConfig(mode="llm", provider="openai", model="stub", sql_fallback=fallback)
    return QueryService(QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=_load_registry(),
        intent_mapper=IntentMapperRouter(config=config, llm_mapper=_RefusingLLM()), intent_config=config)))


def test_a_question_the_layer_cannot_answer_is_answered_with_labelled_generated_sql(monkeypatch):
    import app.services.generated_sql as generated

    monkeypatch.setattr(generated, "build_completion_client",
                        lambda provider: _SqlClient('{"answerable": true, "sql": "SELECT o.customer_state, '
                                                    'COUNT(*) AS n FROM orders o GROUP BY 1"}'))
    response = _service(True).run_question("How many reviews per order?", debug=True)
    assert response.status == "answered" and response.rows
    assert response.interpretation.path == "generated_sql" and "not checked" in response.explanation
    assert any(s["stage"] == "sql_fallback" for s in response.audit["steps"])
    assert response.audit["run"]["sql_fallback"] is True


def test_without_the_flag_or_when_the_model_also_refuses_the_refusal_stands(monkeypatch):
    import app.services.generated_sql as generated

    assert _service(False).run_question("How many reviews per order?").status == "unsupported"
    monkeypatch.setattr(generated, "build_completion_client",
                        lambda provider: _SqlClient('{"answerable": false, "missing": "reviews"}'))
    assert _service(True).run_question("How many reviews per order?").status == "unsupported"
