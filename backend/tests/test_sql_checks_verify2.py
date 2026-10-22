"""Second freeze verification: one ordinary composite-key blocker, plus known limits.

Entirely offline. B1 was a strict xfail (only the missing-key protection failure, not fixture/setup
failures); fixed on 2026-10-20 and kept as a regression test. Conservative refusals are recorded as ordinary behavioral tests.
"""
import duckdb
import pytest

from app.semantic.loader import SemanticRegistry
from app.semantic.models import SemanticSchema
from app.services.sql_checks import check_sql
from tests.test_sql_checks import REGISTRY, COLUMNS, RESTAURANTS, RESTAURANT_COLUMNS, _Index, RN


COMPOSITE = SemanticRegistry(SemanticSchema.model_validate({
    'version': '1', 'dataset': 'composite-key', 'description': '',
    'tables': [{'name': 'payments'}, {'name': 'orders'}],
    'joins': [{'from': 'payments', 'to': 'orders', 'type': 'INNER', 'relationship': 'many_to_one',
               'on': 'payments.account_id = orders.account_id AND payments.order_id = orders.order_id'}],
    'metrics': [{'name': 'revenue', 'display_name': 'Revenue', 'description': '', 'aggregation': 'SUM',
                 'sql_expression': 'payments.amount', 'required_tables': ['payments']}],
    'dimensions': [{'name': 'segment', 'display_name': 'Segment', 'sql_expression': 'orders.segment',
                    'required_tables': ['orders']}], 'time_dimensions': []}))
COMPOSITE_COLUMNS = {'payments': {'account_id', 'order_id', 'amount'},
                     'orders': {'account_id', 'order_id', 'segment'}}
PARTIAL = ('SELECT o.segment, SUM(p.amount) AS revenue FROM payments p '
           'JOIN orders o ON o.order_id=p.order_id GROUP BY o.segment ORDER BY o.segment')
COMPLETE = PARTIAL.replace('ON o.order_id=p.order_id',
                           'ON o.order_id=p.order_id AND o.account_id=p.account_id')


class MissingCompositeKeyAccepted(AssertionError):
    pass


def test_incomplete_composite_join_cannot_silently_duplicate_revenue():
    db = duckdb.connect()
    probes = []
    try:
        db.execute('CREATE TABLE orders(account_id INT, order_id INT, segment VARCHAR, PRIMARY KEY(account_id,order_id))')
        db.execute('CREATE TABLE payments(account_id INT, order_id INT, amount INT, '
                   'FOREIGN KEY(account_id,order_id) REFERENCES orders(account_id,order_id))')
        db.execute("INSERT INTO orders VALUES (1,100,'retail'),(2,100,'business')")
        db.execute("INSERT INTO payments VALUES (1,100,10),(2,100,20)")
        assert db.execute(COMPLETE).fetchall() == [('business', 20), ('retail', 10)]
        assert db.execute(PARTIAL).fetchall() == [('business', 30), ('retail', 30)]
        def run(sql):
            probes.append(sql)
            return db.execute(sql).fetchdf().to_dict('records')
        assert check_sql(COMPLETE, COMPOSITE, COMPOSITE_COLUMNS, run=run).repair is None
        verdict = check_sql(PARTIAL, COMPOSITE, COMPOSITE_COLUMNS, run=run)
        if verdict.repair is None:
            raise MissingCompositeKeyAccepted(f'Wrong groups accepted; multiplicity probes issued: {len(probes)}')
    finally:
        db.close()


def test_using_can_conservatively_refuse_duplicate_base_records_even_without_join_fanout():
    db = duckdb.connect()
    try:
        db.execute('CREATE TABLE payments(account_id INT, order_id INT, amount INT)')
        db.execute('CREATE TABLE orders(account_id INT, order_id INT, segment VARCHAR)')
        db.execute('INSERT INTO payments VALUES (1,100,10),(1,100,10)')
        db.execute("INSERT INTO orders VALUES (1,100,'retail')")
        sql = ('SELECT SUM(p.amount) FROM payments p JOIN orders o USING(account_id,order_id)')
        assert db.execute(sql).fetchone()[0] == 20
        verdict = check_sql(sql, COMPOSITE, COMPOSITE_COLUMNS,
                            run=lambda probe: db.execute(probe).fetchdf().to_dict('records'))
        assert verdict.reason == 'fan_out'  # known conservative refusal: raw duplicates are indistinguishable
    finally:
        db.close()


def test_scalar_subquery_selected_value_is_a_documented_conservative_refusal():
    sql = ("SELECT COUNT(DISTINCT o.order_id) FROM orders o WHERE o.customer_id = "
           "(SELECT MIN(c.customer_id) FROM customers c WHERE c.customer_state='RN')")
    assert check_sql(sql, REGISTRY, COLUMNS, values=_Index(), decisions=RN).reason == 'no_match'


def test_unused_cte_literal_is_a_documented_constructed_limit_not_scope_proof():
    sql = ("WITH unused AS (SELECT customer_id FROM customers WHERE customer_state='RN') "
           "SELECT COUNT(DISTINCT order_id) FROM orders")
    assert check_sql(sql, REGISTRY, COLUMNS, values=_Index(), decisions=RN).repair is None


def test_singleton_cte_total_is_a_documented_conservative_refusal():
    sql = ("WITH t AS (SELECT AVG(cost) AS overall FROM restaurants WHERE currency='INR') "
           "SELECT r.country, AVG(r.cost) / MAX(t.overall) FROM restaurants r CROSS JOIN t "
           "WHERE r.currency='INR' GROUP BY 1")
    assert check_sql(sql, RESTAURANTS, RESTAURANT_COLUMNS).reason == 'fan_out'


def test_a_complete_composite_key_in_any_order_is_declared_and_a_missing_part_is_not():
    reversed_complete = ('SELECT o.segment, SUM(p.amount) FROM payments p JOIN orders o '
                         'ON p.order_id = o.order_id AND o.account_id = p.account_id GROUP BY o.segment')
    assert check_sql(reversed_complete, COMPOSITE, COMPOSITE_COLUMNS).repair is None  # no probe needed
    assert 'join keys' in check_sql(PARTIAL, COMPOSITE, COMPOSITE_COLUMNS).repair  # no data to prove it
