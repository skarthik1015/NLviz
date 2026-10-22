"""Offline close variants for the grouped-composite-key fix in 0507965."""
import pytest

from app.semantic.loader import SemanticRegistry
from app.semantic.models import SemanticSchema
from app.services.sql_checks import check_sql
from tests.test_sql_checks_verify2 import COMPOSITE, COMPOSITE_COLUMNS


def _alternatives():
    raw = COMPOSITE.schema.model_dump(mode='json', by_alias=True)
    raw['joins'].append({'from': 'payments', 'to': 'orders', 'type': 'INNER', 'relationship': 'many_to_one',
                        'on': 'payments.region_id=orders.region_id AND payments.external_id=orders.external_id'})
    columns = {table: {*names, 'region_id', 'external_id'} for table, names in COMPOSITE_COLUMNS.items()}
    return SemanticRegistry(SemanticSchema.model_validate(raw)), columns


@pytest.mark.parametrize('condition', [
    'p.account_id=o.account_id AND p.order_id=o.order_id',
    'o.order_id=p.order_id AND o.account_id=p.account_id',
    'p.region_id=o.region_id AND p.external_id=o.external_id',
    'o.external_id=p.external_id AND o.region_id=p.region_id',
])
def test_either_complete_alternative_key_needs_no_probe(condition):
    registry, columns = _alternatives()
    def unexpected_probe(sql):
        pytest.fail('A complete declared key unexpectedly required a data probe')
    sql = f'SELECT SUM(p.amount) FROM payments p JOIN orders o ON {condition}'
    assert check_sql(sql, registry, columns, run=unexpected_probe).repair is None


def test_fragments_from_different_alternative_keys_are_not_one_complete_key():
    registry, columns = _alternatives()
    sql = ('SELECT SUM(p.amount) FROM payments p JOIN orders o '
           'ON p.account_id=o.account_id AND p.external_id=o.external_id')
    assert check_sql(sql, registry, columns).reason == 'fan_out'
    probes = []
    def repeats(probe):
        probes.append(probe)
        return [{'copies': 2}]
    assert check_sql(sql, registry, columns, run=repeats).reason == 'fan_out'
    assert probes


@pytest.mark.parametrize('prefix', [
    'WITH p AS (SELECT account_id, order_id, amount FROM payments)',
    'WITH raw AS (SELECT * FROM payments), p AS (SELECT * FROM raw)',
])
def test_unchanged_composite_key_through_cte_remains_complete(prefix):
    sql = prefix + (' SELECT SUM(p.amount) FROM p JOIN orders o '
                    'ON p.order_id=o.order_id AND p.account_id=o.account_id')
    assert check_sql(sql, COMPOSITE, COMPOSITE_COLUMNS).repair is None
    partial = prefix + ' SELECT SUM(p.amount) FROM p JOIN orders o ON p.order_id=o.order_id'
    assert check_sql(partial, COMPOSITE, COMPOSITE_COLUMNS).reason == 'fan_out'


def test_renamed_composite_columns_through_cte_remain_complete():
    prefix = ('WITH p AS (SELECT account_id AS account, order_id AS oid, amount AS value FROM payments) ')
    complete = prefix + ('SELECT SUM(p.value) FROM p JOIN orders o '
                         'ON p.oid=o.order_id AND p.account=o.account_id')
    assert check_sql(complete, COMPOSITE, COMPOSITE_COLUMNS).repair is None
    partial = prefix + 'SELECT SUM(p.value) FROM p JOIN orders o ON p.oid=o.order_id'
    assert check_sql(partial, COMPOSITE, COMPOSITE_COLUMNS).reason == 'fan_out'


def test_complete_composite_comma_join_is_recognized():
    sql = ('SELECT SUM(p.amount) FROM payments p, orders o '
           'WHERE o.account_id=p.account_id AND o.order_id=p.order_id')
    assert check_sql(sql, COMPOSITE, COMPOSITE_COLUMNS).repair is None
