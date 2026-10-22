"""Close variants of the definition review (V1-V4). They were strict xfails; fixed 2026-10-21 and kept as tests."""
import pytest

from app.services.sql_checks import check_sql
from tests.test_definitions import app, EVERY_ROW, FILTERED, SHOP, SHOP_COLUMNS, PAID, _statuses
from tests.test_definitions_review import _decision
from tests.test_sql_checks import REGISTRY, COLUMNS


def test_excluding_canceled_does_not_authorize_unfiltered_override():
    verdict = check_sql(EVERY_ROW, REGISTRY, COLUMNS, ask_definitions=True,
                        decisions=_decision("orders.order_status NOT IN ('canceled', 'unavailable')"),
                        overridden={"total_revenue"}, question="Total revenue excluding canceled orders")
    assert verdict.repair is not None or verdict.clarification is not None


def test_total_paid_does_not_silently_choose_paid_status():
    verdict = check_sql(PAID + " WHERE o.status = 'paid'", SHOP, SHOP_COLUMNS,
                        ask_definitions=True, question="What is total paid?", run=_statuses("paid", "failed"))
    assert verdict.clarification is not None


def test_chosen_paid_status_is_accepted_when_also_named_by_question():
    decision = [{"reason": "definition", "constraint": {
        "key": "payments.amount|orders.status", "filter": "orders.status = 'paid'"}}]
    verdict = check_sql(PAID + " WHERE o.status = 'paid'", SHOP, SHOP_COLUMNS, ask_definitions=True,
                        decisions=decision, question="Total paid on paid orders", run=_statuses("paid", "failed"))
    assert verdict.repair is None and verdict.clarification is None


def test_model_invented_delivered_subset_does_not_prove_definition_immaterial(app):
    sql = FILTERED.split(" WHERE ")[0] + " WHERE o.order_status = 'delivered'"
    service, _, _ = app(sql)
    assert service.run_question("Total revenue", conversation_id="narrower-probe").status == "clarification"


def test_failed_remember_does_not_confirm_definition_for_new_conversation(app):
    class FailingStorage:
        def read_text(self, *args):
            raise FileNotFoundError

        def write_text(self, *args):
            raise OSError("simulated disk failure")

    service, _, _ = app(EVERY_ROW, FILTERED, FILTERED, storage=FailingStorage())
    assert service.run_question("Total revenue", conversation_id="first").status == "clarification"
    assert service.resume("first", "0", remember=True).status == "answered"
    assert service.run_question("Total revenue", conversation_id="new").status == "clarification"
