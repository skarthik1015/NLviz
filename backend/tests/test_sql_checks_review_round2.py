"""Independent E round-two cases. They were strict xfails documenting the findings; all six
are fixed (2026-10-19) and the cases now pass as regression tests.

Offline: no LLM or Jammi calls.
"""
import pytest
import sqlglot
from sqlglot import exp

from app.services.sql_checks import check_sql, _probe_value
from tests.test_sql_checks import REGISTRY, COLUMNS, RESTAURANTS, RESTAURANT_COLUMNS, STATE, _Index

def test_transformed_cte_must_not_claim_original_metric():
    sql = ("WITH p AS (SELECT op.payment_value * 100 AS amount FROM order_payments op "
           "JOIN orders o ON o.order_id=op.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable')) "
           "SELECT SUM(amount) FROM p")
    verdict = check_sql(sql, REGISTRY, COLUMNS)
    assert not any(m['name'] == 'total_revenue' for m in verdict.measures)

def test_preaggregation_does_not_make_subsequent_fanout_safe():
    sql = ("WITH p AS (SELECT op.order_id, SUM(op.payment_value) AS amount FROM order_payments op "
           "JOIN orders o ON o.order_id=op.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') "
           "GROUP BY op.order_id) SELECT SUM(p.amount) FROM p JOIN order_items oi ON oi.order_id=p.order_id")
    assert check_sql(sql, REGISTRY, COLUMNS).repair

def test_filtering_other_order_does_not_filter_payment_order():
    sql = ("SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id=op.order_id "
           "JOIN orders other ON other.order_id=other.order_id "
           "WHERE other.order_status NOT IN ('canceled', 'unavailable')")
    assert check_sql(sql, REGISTRY, COLUMNS).repair


@pytest.mark.parametrize('suffix', ["c.customer_state='SP'", '1=1'])
def test_selected_value_must_be_present_in_regenerated_sql(suffix):
    decisions = [{'reason': 'no_match', 'constraint': {
        'kind': 'value', 'column': 'customers.customer_state', 'raw': 'Rio', 'value': 'RN'}}]
    assert check_sql(STATE + suffix, REGISTRY, COLUMNS, values=_Index(), decisions=decisions).repair

def test_nullif_nested_in_coalesce_does_not_exclude_zeros():
    v = check_sql('SELECT AVG(COALESCE(NULLIF(rating, 0), 0)) FROM restaurants',
                  RESTAURANTS, RESTAURANT_COLUMNS)
    assert v.clarification

def test_unknown_choice_must_not_become_first_choice(monkeypatch):
    from app.agent.nodes import sql_primary
    monkeypatch.setattr(sql_primary, 'interrupt', lambda pending: 'not-a-choice')
    result = sql_primary.build_sql_clarify_node()({'pending_clarification': {
        'reason': 'mixed_units', 'choices': [{'id': 'by', 'label': 'Currency',
            'instruction': 'group by currency', 'constraint': {'kind': 'group'}}]}})
    assert not result.get('sql_answered')

def test_probe_quotes_text_as_literal_and_does_not_add_statements():
    seen = []
    def run(sql):
        seen.append(sql)
        return []
    raw = "O'Reilly'; DROP TABLE restaurants; --"
    _probe_value(None, exp.column('country'), ('restaurants', 'country'), raw, run)
    assert len(seen) == 2
    for sql in seen:
        trees = sqlglot.parse(sql, read='postgres')
        assert len(trees) == 1 and isinstance(trees[0], exp.Select)
    assert any(l.this == raw for l in sqlglot.parse_one(seen[0], read='postgres').find_all(exp.Literal))
