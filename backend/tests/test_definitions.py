"""Definition clarifications on the SQL path (DEFINITION_CLARIFICATIONS): an inferred condition that
changes the answer is asked about before anything runs, unless the question, this conversation or
the owner's confirmation for the connection settles it. Runs on the DuckDB demo data, where total
revenue's inferred condition (order status not canceled/unavailable) leaves out real orders."""
from __future__ import annotations

import json

import pytest

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.semantic.loader import SemanticRegistry
from app.semantic.models import SemanticSchema
from app.services import IntentMapperConfig, IntentMapperRouter, QueryService
from app.services.definitions import DefinitionStore
from app.services.sql_checks import check_sql
from app.storage.schema_storage import LocalSchemaStorage

from tests.test_query_graph import ExplodingMapper, _load_registry
from tests.test_sql_checks import COLUMNS, REGISTRY

FILTERED = ("SELECT SUM(op.payment_value) AS revenue FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
            "WHERE o.order_status NOT IN ('canceled', 'unavailable')")
EVERY_ROW = "SELECT SUM(op.payment_value) AS revenue FROM order_payments op"


class _Script:
    def __init__(self, *sqls):
        self.sqls = list(sqls)
        self.prompts: list[str] = []

    def complete_json(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        return json.dumps({"answerable": True, "sql": self.sqls.pop(0)})


@pytest.fixture
def app(monkeypatch, tmp_path):
    import app.services.generated_sql as generated

    def make(*sqls, connection_id="conn-a", storage=None):
        script = _Script(*sqls)
        monkeypatch.setattr(generated, "build_completion_client", lambda provider: script)
        connector = DuckDBConnector()
        config = IntentMapperConfig(mode="llm", provider="openai", model="stub", answer_path="sql",
                                    ask_definitions=True)
        store = DefinitionStore(storage or LocalSchemaStorage(tmp_path), connection_id)
        service = QueryService(QueryGraphRunner(QueryGraphDependencies(
            connector=connector, schema_context=connector.get_schema(), registry=_load_registry(),
            intent_mapper=IntentMapperRouter(config=config, llm_mapper=ExplodingMapper()), intent_config=config,
            definitions=store)))
        return service, script, store

    return make


def test_an_unconfirmed_condition_that_changes_the_answer_is_asked_before_running(app):
    service, script, _ = app(EVERY_ROW)
    asked = service.run_question("Total revenue", conversation_id="c1")
    assert asked.status == "clarification" and asked.clarification.reason == "definition"
    assert asked.rows == [] and asked.sql is None  # nothing was answered
    labels = [c.label for c in asked.clarification.choices]
    assert labels[0].startswith("Leave out canceled, unavailable") and "guess" in labels[0]
    assert labels[1] == "Count every row (no condition)"
    assert asked.clarification.rememberable


def test_each_answer_produces_its_own_calculation(app):
    service, script, _ = app(EVERY_ROW, FILTERED)
    service.run_question("Total revenue", conversation_id="c1")
    leave_out = service.resume("c1", "0")
    assert "NOT o.order_status IN ('canceled', 'unavailable')" in leave_out.sql
    assert "only where orders.order_status NOT IN" in script.prompts[1]

    service, script, _ = app(EVERY_ROW, FILTERED, EVERY_ROW)
    service.run_question("Total revenue", conversation_id="c2")
    every_row = service.resume("c2", "1")  # the model first keeps the condition: sent back
    assert every_row.status == "answered" and "order_status" not in every_row.sql
    assert every_row.rows[0]["revenue"] > leave_out.rows[0]["revenue"]
    assert "every row" in script.prompts[1] and "remove" in script.prompts[2]
    policy = every_row.interpretation.policies[0]
    assert (policy.source, policy.statement) == ("conversation", "Count every row (no condition)")
    # The panel describes what ran: not the data model's "non-canceled orders" text.
    metric = every_row.interpretation.metric
    assert metric.definition.endswith("over every row") and metric.description == ""
    kept = leave_out.interpretation.metric
    assert "where orders.order_status NOT IN" in kept.definition and kept.description
    assert "your choice in this conversation" in every_row.explanation


def test_follow_ups_keep_the_choice_and_a_new_conversation_asks_again(app):
    service, _, _ = app(EVERY_ROW, FILTERED, FILTERED, EVERY_ROW)
    service.run_question("Total revenue", conversation_id="c1")
    service.resume("c1", "0")
    follow_up = service.run_question("and the total again?", conversation_id="c1")
    assert follow_up.status == "answered" and follow_up.interpretation.policies[0].source == "conversation"
    assert service.run_question("Total revenue", conversation_id="c2").status == "clarification"


def test_a_confirmed_definition_is_used_without_asking_and_stays_with_its_connection(app, tmp_path):
    storage = LocalSchemaStorage(tmp_path)
    service, _, store = app(EVERY_ROW, FILTERED, FILTERED, storage=storage)
    service.run_question("Total revenue", conversation_id="c1")
    service.resume("c1", "0", remember=True)
    assert store.all()["order_payments.payment_value"]["filter"] == "orders.order_status NOT IN ('canceled', 'unavailable')"
    again = service.run_question("Total revenue", conversation_id="c-new")
    assert again.status == "answered" and again.interpretation.policies[0].source == "confirmed"
    # Survives a restart; another connection on the same storage does not see it.
    assert DefinitionStore(storage, "conn-a").all() == store.all()
    other, _, _ = app(EVERY_ROW, connection_id="conn-b", storage=storage)
    assert other.run_question("Total revenue", conversation_id="c1").status == "clarification"


def test_a_choice_not_remembered_is_not_saved(app):
    service, _, store = app(EVERY_ROW, FILTERED)
    service.run_question("Total revenue", conversation_id="c1")
    service.resume("c1", "0")
    assert store.all() == {}


def test_a_condition_that_leaves_out_nothing_here_is_not_asked_about():
    verdict = check_sql(FILTERED, REGISTRY, COLUMNS, ask_definitions=True, run=lambda probe: [{"excluded": 0}])
    assert verdict.clarification is None and verdict.repair is None
    assert "leaves out no rows" in verdict.notes[0]


def test_the_question_overriding_a_definition_is_not_asked_about():
    verdict = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True, overridden={"total_revenue"},
                        run=lambda probe: [{"excluded": 5}])
    assert verdict.clarification is None and verdict.policies[0]["source"] == "question"


def test_a_confirmation_made_among_other_options_is_asked_again():
    stale = {"order_payments.payment_value": {"filter": "orders.order_status = 'delivered'"}}
    verdict = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True, confirmed=stale,
                        run=lambda probe: [{"excluded": 5}])
    assert verdict.clarification["reason"] == "definition"


# Two definitions of the same amount that disagree.
CONFLICT = SemanticRegistry(SemanticSchema.model_validate({
    "version": "1", "dataset": "c", "description": "", "tables": [{"name": "sales"}], "joins": [],
    "metrics": [
        {"name": "net_sales", "display_name": "Net Sales", "description": "", "aggregation": "SUM",
         "sql_expression": "sales.amount", "required_tables": ["sales"], "base_filter": "sales.status <> 'refunded'"},
        {"name": "booked_sales", "display_name": "Booked Sales", "description": "", "aggregation": "SUM",
         "sql_expression": "sales.amount", "required_tables": ["sales"], "base_filter": "sales.status = 'booked'"},
    ],
    "dimensions": [], "time_dimensions": []}))


def test_conflicting_definitions_are_asked_about_even_without_data():
    verdict = check_sql("SELECT SUM(amount) FROM sales", CONFLICT, {"sales": {"amount", "status"}}, ask_definitions=True)
    labels = [c["label"] for c in verdict.clarification["choices"]]
    assert labels == ["Leave out refunded (status)", "Only booked (status)", "Count every row (no condition)"]
    assert "disagree" in verdict.clarification["prompt"]
    chose = [{"reason": "definition", "constraint": verdict.clarification["choices"][1]["constraint"]}]
    assert check_sql("SELECT SUM(amount) FROM sales WHERE status = 'booked'", CONFLICT,
                     {"sales": {"amount", "status"}}, ask_definitions=True, decisions=chose).repair is None
    assert "status = 'booked'" in check_sql("SELECT SUM(amount) FROM sales", CONFLICT, {"sales": {"amount", "status"}},
                                            ask_definitions=True, decisions=chose).repair


def test_a_grouped_number_code_is_a_chart_label():
    from app.agent.nodes.sql_primary import _chart, _grouped
    from app.models import SemanticIntent

    rows = [{"payment_type": 1, "metric_value": 43.0}, {"payment_type": 2, "metric_value": 7.8}]
    spec = _chart(rows, SemanticIntent(metric="fare"), "Total Fare",
                  _grouped("SELECT payment_type, SUM(fare) AS metric_value FROM t GROUP BY 1"))
    assert spec is not None and spec["data"][0]["type"] == "bar" and spec["data"][0]["x"] == ["1", "2"]


# A generated-style layer: no recorded condition, but a status column that splits the amount.
SHOP = SemanticRegistry(SemanticSchema.model_validate({
    "version": "1", "dataset": "shop", "description": "",
    "tables": [{"name": "payments"}, {"name": "orders"}],
    "joins": [{"from": "payments", "to": "orders", "on": "payments.order_id = orders.order_id",
               "relationship": "many_to_one"}],
    "metrics": [
        {"name": "total_paid", "display_name": "Total Paid", "description": "", "aggregation": "SUM",
         "sql_expression": "payments.amount", "required_tables": ["payments"]},
        {"name": "order_count", "display_name": "Orders", "description": "", "aggregation": "COUNT_DISTINCT",
         "sql_expression": "orders.order_id", "required_tables": ["orders"]},
    ],
    "dimensions": [{"name": "order_status", "display_name": "Order Status", "sql_expression": "orders.status",
                    "required_tables": ["orders"]}],
    "time_dimensions": []}))
SHOP_COLUMNS = {"payments": {"order_id", "amount"}, "orders": {"order_id", "status"}}
PAID = "SELECT SUM(p.amount) FROM payments p JOIN orders o ON o.order_id = p.order_id"


def _statuses(*values):
    return lambda probe: [{"value": v, "n": 10 - i} for i, v in enumerate(values)]


def test_a_status_that_splits_an_amount_is_asked_about_with_its_real_values():
    verdict = check_sql(PAID, SHOP, SHOP_COLUMNS, ask_definitions=True, run=_statuses("paid", "refunded", "failed"))
    choice = verdict.clarification
    assert [c["label"] for c in choice["choices"]] == ["Count every Order Status", "Only paid", "Only refunded",
                                                         "Only failed"]
    assert "Nothing says which should count" in choice["prompt"] and choice["rememberable"]


def test_the_status_choice_is_enforced_either_way():
    run = _statuses("paid", "refunded")
    choice = check_sql(PAID, SHOP, SHOP_COLUMNS, ask_definitions=True, run=run).clarification
    every = [{"reason": "definition", "constraint": choice["choices"][0]["constraint"]}]
    only_paid = [{"reason": "definition", "constraint": choice["choices"][1]["constraint"]}]
    filtered = PAID + " WHERE o.status = 'paid'"
    assert check_sql(PAID, SHOP, SHOP_COLUMNS, ask_definitions=True, run=run, decisions=every).repair is None
    assert "remove" in check_sql(filtered, SHOP, SHOP_COLUMNS, ask_definitions=True, run=run, decisions=every).repair
    assert "orders.status = 'paid'" in check_sql(PAID, SHOP, SHOP_COLUMNS, ask_definitions=True, run=run,
                                                 decisions=only_paid).repair
    done = check_sql(filtered, SHOP, SHOP_COLUMNS, ask_definitions=True, run=run, decisions=only_paid)
    assert done.repair is None and done.policies[0]["source"] == "conversation"


def test_no_status_question_when_the_query_names_a_status_holds_one_value_or_only_counts():
    # Naming a status in the question does not authorize it either: one click confirms it.
    named = PAID + " WHERE o.status = 'paid'"
    asked = check_sql(named, SHOP, SHOP_COLUMNS, ask_definitions=True, run=_statuses("paid", "refunded"),
                      question="Total paid on paid orders")
    assert asked.clarification is not None
    assert check_sql(PAID, SHOP, SHOP_COLUMNS, ask_definitions=True, run=_statuses("paid")).clarification is None
    count = "SELECT COUNT(DISTINCT o.order_id) FROM orders o"
    assert check_sql(count, SHOP, SHOP_COLUMNS, ask_definitions=True, run=_statuses("paid", "refunded")).clarification is None


def test_a_status_on_a_parent_table_the_query_does_not_join_is_still_asked_about():
    probes = []

    def run(probe):
        probes.append(probe)
        return [{"value": "paid", "n": 5}, {"value": "refunded", "n": 2}]

    verdict = check_sql("SELECT SUM(amount) FROM payments", SHOP, SHOP_COLUMNS, ask_definitions=True, run=run)
    assert verdict.clarification["reason"] == "definition"
    assert "FROM orders" in probes[-1]


def test_a_confirmed_status_choice_is_used_without_asking():
    key = "payments.amount|orders.status"
    confirmed = {key: {"filter": None, "label": "Every Order Status counted"}}
    verdict = check_sql(PAID, SHOP, SHOP_COLUMNS, ask_definitions=True, run=_statuses("paid", "refunded"),
                        confirmed=confirmed)
    assert verdict.clarification is None and verdict.policies[0]["source"] == "confirmed"


def test_quantities_are_not_asked_about_statuses():
    units = SemanticRegistry(SemanticSchema.model_validate({
        "version": "1", "dataset": "u", "description": "", "tables": [{"name": "lines"}], "joins": [],
        "metrics": [{"name": "total_quantity", "display_name": "Total Quantity", "description": "",
                     "aggregation": "SUM", "sql_expression": "lines.quantity", "required_tables": ["lines"]}],
        "dimensions": [{"name": "line_status", "display_name": "Line Status", "sql_expression": "lines.status",
                        "required_tables": ["lines"]}], "time_dimensions": []}))
    verdict = check_sql("SELECT SUM(quantity) FROM lines", units, {"lines": {"quantity", "status"}},
                        ask_definitions=True, run=_statuses("O", "F"))
    assert verdict.clarification is None


def test_wording_never_overrides_an_answered_definition():
    """The question's words and the model's claim are not authorization ("excluding
    canceled" names canceled too). When the model says the question asks for something else, the
    user is asked again, with the earlier answer shown; with no question to check, it is enforced."""
    decided = [{"reason": "definition", "earlier": True,
                "constraint": {"key": "order_payments.payment_value",
                               "filter": "orders.order_status NOT IN ('canceled', 'unavailable')"}}]
    verdict = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True, decisions=decided,
                        overridden={"total_revenue"}, question="Total revenue including canceled orders")
    assert verdict.clarification["prompt"].startswith("Earlier, you chose: Leave out canceled")
    enforced = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True, decisions=decided, overridden={"total_revenue"})
    assert enforced.repair is not None


def test_a_conflicting_follow_up_is_asked_again_once_then_the_new_answer_holds(app):
    from tests.test_definitions import _Script  # noqa: F401 - the fixture's scripted model

    service, script, _ = app(EVERY_ROW, FILTERED, EVERY_ROW, EVERY_ROW)
    service.run_question("Total revenue", conversation_id="c1")
    service.resume("c1", "0")  # leave out canceled
    import app.services.generated_sql as generated

    claims = iter([["total_revenue"], ["total_revenue"]])
    original = script.complete_json

    def claiming(**kwargs):
        import json as _json
        reply = _json.loads(original(**kwargs))
        reply["definitions_overridden"] = next(claims, [])
        return _json.dumps(reply)

    script.complete_json = claiming
    again = service.run_question("Total revenue including canceled orders", conversation_id="c1")
    assert again.status == "clarification" and again.clarification.prompt.startswith("Earlier, you chose")
    answered = service.resume("c1", "1")  # count every row; the model still claims: no second question
    assert answered.status == "answered" and "order_status" not in answered.sql
    assert answered.interpretation.policies[0].source == "conversation"
