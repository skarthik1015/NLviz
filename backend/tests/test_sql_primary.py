"""The SQL-primary path (ANSWER_PATH=sql): the model's SQL is held to the semantic layer, sent back
when a check fails, and explained from the SQL that ran. Runs on the DuckDB demo data."""
from __future__ import annotations

import json

import pytest

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.services import IntentMapperConfig, IntentMapperRouter, QueryService
from app.services.intent_mapper import _answer_path

from tests.test_query_graph import ExplodingMapper, _load_registry

DOUBLE_COUNTED = ("SELECT p.product_category_name_english AS category, SUM(op.payment_value) AS revenue "
                  "FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
                  "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id "
                  "WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY 1")
NO_CONDITION = "SELECT c.customer_state, SUM(op.payment_value) AS revenue FROM order_payments op " \
               "JOIN orders o ON o.order_id = op.order_id JOIN customers c ON c.customer_id = o.customer_id GROUP BY 1"
GOOD = ("SELECT c.customer_state, SUM(op.payment_value) AS revenue FROM order_payments op "
        "JOIN orders o ON o.order_id = op.order_id JOIN customers c ON c.customer_id = o.customer_id "
        "WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY 1 ORDER BY 2 DESC LIMIT 5")


class _Script:
    """The SQL model: one scripted reply per call; records the prompts it got."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete_json(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        return json.dumps(self.replies.pop(0))


@pytest.fixture
def ask(monkeypatch):
    import app.services.generated_sql as generated

    def make(*replies):
        script = _Script(*replies)
        monkeypatch.setattr(generated, "build_completion_client", lambda provider: script)
        connector = DuckDBConnector()
        config = IntentMapperConfig(mode="llm", provider="openai", model="stub", answer_path="sql")
        mapper = IntentMapperRouter(config=config, llm_mapper=ExplodingMapper())
        service = QueryService(QueryGraphRunner(QueryGraphDependencies(
            connector=connector, schema_context=connector.get_schema(), registry=_load_registry(),
            intent_mapper=mapper, intent_config=config)))
        return service, script

    return make


def test_double_counting_and_a_missing_definition_go_back_to_the_model(ask):
    service, script = ask({"answerable": True, "sql": DOUBLE_COUNTED}, {"answerable": True, "sql": NO_CONDITION},
                          {"answerable": True, "sql": GOOD})
    response = service.run_question("Revenue by state, top 5", debug=True)
    assert response.status == "answered" and len(response.rows) == 5
    checks = [s.get("check") for s in response.audit["steps"] if s["stage"] == "rejected"]
    assert checks == ["fan_out", "policy"]
    assert "order_items" in script.prompts[1] and "order_status NOT IN" in script.prompts[2]
    panel = response.interpretation
    assert panel.path == "sql" and panel.metric.name == "total_revenue" and panel.sql == response.sql
    assert panel.grouping[0].label == "Customer State" and panel.limit == 5
    assert panel.policies[0].name == "total_revenue"
    assert response.audit["run"]["answer_path"] == "sql"
    assert response.chart_spec is not None


def test_sql_that_keeps_failing_a_check_is_refused_with_the_reason(ask):
    service, _ = ask(*[{"answerable": True, "sql": DOUBLE_COUNTED}] * 3)
    response = service.run_question("Revenue by product category")
    assert response.status == "unsupported" and "order_items" in response.intent.unsupported


def test_a_definition_the_question_overrides_is_kept_and_said(ask):
    service, _ = ask({"answerable": True, "sql": NO_CONDITION, "definitions_overridden": ["total_revenue"]})
    response = service.run_question("Revenue by state including canceled orders")
    assert response.status == "answered"
    assert any("does not apply that condition" in note for note in response.interpretation.notes)


def test_the_model_saying_the_data_cannot_answer_is_a_refusal(ask):
    service, _ = ask({"answerable": False, "missing": "weather data"})
    response = service.run_question("Will it rain tomorrow?")
    assert response.status == "unsupported" and response.sql is None


def test_a_follow_up_starts_from_the_previous_sql(ask):
    service, script = ask({"answerable": True, "sql": GOOD}, {"answerable": True, "sql": GOOD})
    service.run_question("Revenue by state, top 5", conversation_id="c1")
    service.run_question("and the top 3?", conversation_id="c1")
    previous = script.prompts[1].split("Its SQL: ")[1]
    assert "Previous question in this conversation: Revenue by state, top 5" in script.prompts[1]
    assert "LIMIT 5" in previous  # the SQL that ran (as the guard wrote it)


def test_the_switch_is_validated_and_sql_is_the_apps_default(monkeypatch):
    monkeypatch.delenv("ANSWER_PATH", raising=False)
    assert IntentMapperConfig.from_env().answer_path == "sql"
    assert _answer_path("SQL") == "sql" and _answer_path("") == "semantic"
    with pytest.raises(ValueError):
        _answer_path("magic")


def test_heuristic_mode_keeps_the_compiler():
    connector = DuckDBConnector()
    config = IntentMapperConfig(mode="heuristic", answer_path="sql")
    runner = QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=_load_registry(),
        intent_mapper=IntentMapperRouter(config=config), intent_config=config))
    assert runner.run_info["answer_path"] == "semantic"
    response = QueryService(runner).run_question("Show revenue by customer state")
    assert response.status == "answered" and response.interpretation.path == "semantic"


def test_a_value_no_row_has_is_asked_about_and_the_answer_is_used(ask):
    rio = ("SELECT COUNT(DISTINCT o.order_id) AS orders FROM orders o JOIN customers c "
           "ON c.customer_id = o.customer_id WHERE c.customer_city = '{}'")
    service, script = ask({"answerable": True, "sql": rio.format("Rio")},
                          {"answerable": True, "sql": rio.format("rio de janeiro")})
    asked = service.run_question("Orders from customers in Rio", conversation_id="rio")
    assert asked.status == "clarification" and asked.clarification.reason == "no_match"
    assert asked.clarification.choices[0].label == "rio de janeiro"
    answered = service.resume("rio", "0")
    assert answered.status == "answered" and answered.rows[0]["orders"] > 0
    assert "The user decided: For 'Rio', use customers.customer_city = 'rio de janeiro'." in script.prompts[1]
    assert any(e.get("value") == "rio de janeiro" and e.get("text") == "Rio" for e in answered.evidence)


def test_an_unknown_answer_to_a_choice_asks_again_and_never_picks_one(ask):
    rio = ("SELECT COUNT(DISTINCT o.order_id) AS orders FROM orders o JOIN customers c "
           "ON c.customer_id = o.customer_id WHERE c.customer_city = '{}'")
    service, script = ask({"answerable": True, "sql": rio.format("Rio")},
                          {"answerable": True, "sql": rio.format("rio de janeiro")})
    service.run_question("Orders from customers in Rio", conversation_id="rio2")
    again = service.resume("rio2", "not-a-choice")
    assert again.status == "clarification" and len(script.prompts) == 1  # no new SQL was asked for
    assert service.resume("rio2", "0").status == "answered"
