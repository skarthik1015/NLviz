"""Review regressions (D1-D7). They were strict xfails; fixed 2026-10-21 and kept as tests. Offline."""
import pytest

from app.services.sql_checks import check_sql
from app.services.interpretation import interpretation_from_sql
from tests.test_definitions import app, EVERY_ROW, FILTERED, SHOP, SHOP_COLUMNS, PAID, _statuses
from tests.test_sql_checks import REGISTRY, COLUMNS


def _decision(condition):
    return [{"reason": "definition", "constraint": {
        "key": "order_payments.payment_value", "filter": condition}}]


def test_definition_memory_does_not_cross_user_connection_namespaces(app):
    service, _, _ = app(EVERY_ROW, FILTERED, EVERY_ROW)
    service.run_question("Total revenue", conversation_id="same-id", scope="owner/connection-a")
    assert service.resume("same-id", "0", scope="owner/connection-a").status == "answered"
    assert service.run_question("Total revenue", conversation_id="same-id",
                                scope="other-owner/connection-a").status == "clarification"


def test_model_override_cannot_discard_an_answered_definition():
    condition = "orders.order_status NOT IN ('canceled', 'unavailable')"
    verdict = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True,
                        decisions=_decision(condition), overridden={"total_revenue"})
    assert verdict.repair is not None


def test_count_every_row_rejects_a_different_filter_on_the_policy_column():
    sql = FILTERED.split(" WHERE ")[0] + " WHERE o.order_status = 'delivered'"
    verdict = check_sql(sql, REGISTRY, COLUMNS, ask_definitions=True, decisions=_decision(None))
    assert verdict.repair is not None


@pytest.mark.parametrize("predicate", ["o.status <> 'failed'", "o.status IS NOT NULL"])
def test_every_status_rejects_non_equality_status_filters(predicate):
    decisions = [{"reason": "definition", "constraint": {"key": "payments.amount|orders.status", "filter": None}}]
    verdict = check_sql(PAID + " WHERE " + predicate, SHOP, SHOP_COLUMNS, ask_definitions=True,
                        decisions=decisions, run=_statuses("paid", "failed"))
    assert verdict.repair is not None


def test_model_invented_status_filter_still_needs_user_authorization():
    verdict = check_sql(PAID + " WHERE o.status = 'paid'", SHOP, SHOP_COLUMNS, ask_definitions=True,
                        run=_statuses("paid", "failed"))
    assert verdict.clarification is not None


def test_definition_on_join_is_not_proof_it_excludes_no_rows(app):
    sql = FILTERED.replace(" WHERE ", " AND ")
    service, _, _ = app(sql)
    result = service.run_question("Total revenue", conversation_id="join-policy")
    assert result.status == "clarification"


def test_panel_does_not_call_a_status_filtered_metric_every_row():
    sql = PAID + " WHERE o.status = 'paid'"
    decisions = [{"reason": "definition", "constraint": {
        "key": "payments.amount|orders.status", "filter": "orders.status = 'paid'"}}]
    verdict = check_sql(sql, SHOP, SHOP_COLUMNS, ask_definitions=True, decisions=decisions,
                        run=_statuses("paid", "failed"))
    assert verdict.repair is None and verdict.clarification is None
    panel = interpretation_from_sql(sql, {"measures": verdict.measures, "policies": verdict.policies}, SHOP, SHOP_COLUMNS)
    assert "over every row" not in panel.metric.definition


def test_panel_hides_filtered_description_when_question_overrides_definition():
    verdict = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True, overridden={"total_revenue"})
    assert verdict.repair is None
    panel = interpretation_from_sql(EVERY_ROW, {"measures": verdict.measures, "policies": verdict.policies},
                                    REGISTRY, COLUMNS)
    assert panel.metric.description == ""


def test_concurrent_confirmations_survive_a_restart():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.services.definitions import DefinitionStore

    entered, release = Event(), Event()

    class Storage:
        content = "{}"

        def read_text(self, *args):
            return self.content

        def write_text(self, connection, name, content):
            import json
            if len(json.loads(content)) == 1:
                entered.set()
                assert release.wait(3)
            self.content = content

    storage = Storage()
    store = DefinitionStore(storage, "same-connection")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.confirm, "amount-a", None, "all", [None])
        try:
            assert entered.wait(3)
            second = pool.submit(store.confirm, "amount-b", None, "all", [None])
            second.result(timeout=3)
        finally:
            release.set()
        first.result(timeout=3)
    assert set(DefinitionStore(storage, "same-connection").all()) == {"amount-a", "amount-b"}
