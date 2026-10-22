"""Freeze verification variants (round two). V1-V5 were strict xfails; fixed on
2026-10-20 and kept as regression tests. No model or live database calls."""
import pytest

from app.services.sql_checks import check_sql
from tests.test_sql_checks import REGISTRY, COLUMNS, RESTAURANTS, RESTAURANT_COLUMNS, STATE, _Index


def test_tautological_join_must_not_pass_as_a_safe_many_to_one_join():
    sql = ("SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id=op.order_id "
           "JOIN customers c ON c.customer_id=o.customer_id OR 1=1 "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    assert check_sql(sql, REGISTRY, COLUMNS).repair


def test_chosen_state_must_filter_the_orders_not_an_unrelated_subquery():
    decisions = [{'reason': 'no_match', 'constraint': {
        'kind': 'value', 'column': 'customers.customer_state', 'raw': 'Rio', 'value': 'RN'}}]
    sql = ("SELECT COUNT(DISTINCT o.order_id) FROM orders o "
           "WHERE EXISTS (SELECT 1 FROM customers c WHERE c.customer_state='RN')")
    assert check_sql(sql, REGISTRY, COLUMNS, values=_Index(), decisions=decisions).repair


def test_any_state_must_not_become_a_different_state_filter():
    decisions = [{'reason': 'no_match', 'constraint': {
        'kind': 'any', 'column': 'customers.customer_state', 'raw': 'Rio', 'value': None}}]
    assert check_sql(STATE + "c.customer_state='SP'", REGISTRY, COLUMNS,
                     values=_Index(), decisions=decisions).repair


@pytest.mark.parametrize('use_alias', [True, False])
def test_rounding_money_to_whole_units_must_not_claim_original_revenue_definition(use_alias):
    table = 'op' if use_alias else 'order_payments'
    source = 'order_payments op' if use_alias else 'order_payments'
    sql = (f"SELECT SUM(CAST({table}.payment_value AS DECIMAL(18,0))) FROM {source} "
           f"JOIN orders o ON o.order_id={table}.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    assert not any(m['name'] == 'total_revenue' for m in check_sql(sql, REGISTRY, COLUMNS).measures)


def test_singleton_cross_join_is_not_refused_as_double_counting():
    sql = "SELECT AVG(r.cost) FROM restaurants r CROSS JOIN (SELECT 1 AS k) one WHERE r.currency='INR'"
    verdict = check_sql(sql, RESTAURANTS, RESTAURANT_COLUMNS)
    assert verdict.repair is None and verdict.clarification is None


def test_small_independent_data_demonstrates_the_accepted_queries_change_answers():
    """Tiny local oracle: never execute the Cartesian example against the full demo database."""
    import duckdb
    db = duckdb.connect()
    try:
        db.execute('CREATE TABLE customers(customer_id INT, customer_state VARCHAR)')
        db.execute("INSERT INTO customers VALUES (1,'RN'), (2,'SP'), (3,'SP')")
        db.execute('CREATE TABLE orders(order_id INT, customer_id INT, order_status VARCHAR)')
        db.execute("INSERT INTO orders VALUES (1,1,'delivered'), (2,2,'delivered')")
        db.execute('CREATE TABLE order_payments(order_id INT, payment_value DECIMAL(18,2))')
        db.execute('INSERT INTO order_payments VALUES (1,10.25), (2,20.25)')
        assert float(db.execute('SELECT SUM(payment_value) FROM order_payments').fetchone()[0]) == 30.5
        multiplied = db.execute(
            'SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id=op.order_id '
            'JOIN customers c ON c.customer_id=o.customer_id OR 1=1').fetchone()[0]
        assert float(multiplied) == 91.5
        assert db.execute("SELECT COUNT(*) FROM orders o WHERE EXISTS "
                          "(SELECT 1 FROM customers c WHERE c.customer_state='RN')").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM orders o JOIN customers c ON c.customer_id=o.customer_id "
                          "WHERE c.customer_state='RN'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM orders o JOIN customers c ON c.customer_id=o.customer_id "
                          "WHERE c.customer_state='SP'").fetchone()[0] == 1  # 'Any' would keep both orders
        assert float(db.execute('SELECT SUM(CAST(payment_value AS DECIMAL(18,0))) FROM order_payments').fetchone()[0]) == 30.0
    finally:
        db.close()
