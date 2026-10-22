"""Join inference: *_id names, and lookup keys of attached files."""
from __future__ import annotations

from app.connectors.join_inference import infer_joins

TABLES = {
    "trips": [{"name": "vendorid"}, {"name": "pulocationid"}, {"name": "dolocationid"}, {"name": "fare_amount"}],
    "taxi_zone_lookup": [{"name": "locationid"}, {"name": "borough"}],
}
ROWS = {"trips": 2964624, "taxi_zone_lookup": 265}
DISTINCT = {"trips": {"vendorid": 3, "pulocationid": 260, "dolocationid": 261},
            "taxi_zone_lookup": {"locationid": 265, "borough": 7}}


def test_a_column_ending_in_a_lookups_unique_key_joins_it():
    joins, provenance = infer_joins(TABLES, set(TABLES), DISTINCT, ROWS)
    assert joins == [{"from": "trips", "to": "taxi_zone_lookup", "on": "trips.pulocationid = taxi_zone_lookup.locationid",
                      "type": "LEFT", "relationship": "many_to_one"}]
    assert "also matched dolocationid" in provenance[joins[0]["on"]]


def test_no_lookup_join_without_counts_or_when_the_key_is_not_unique():
    assert infer_joins(TABLES, set(TABLES))[0] == []
    assert infer_joins(TABLES, set(TABLES), {**DISTINCT, "taxi_zone_lookup": {"locationid": 100}}, ROWS)[0] == []


def test_fused_lowercase_ids_are_ids_but_words_ending_in_id_are_not():
    from app.services.schema_completion import _ID_LIKE

    assert all(_ID_LIKE.search(n) for n in ("vendorid", "pulocationid", "ratecodeid", "customer_id", "c_custkey"))
    assert not any(_ID_LIKE.search(n) for n in ("amount_paid", "paid", "valid", "is_valid", "rapid", "fare_amount"))
