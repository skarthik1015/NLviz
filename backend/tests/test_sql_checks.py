"""The semantic protections on SQL the model writes (app.services.sql_checks): double counting,
agreed definitions, mixed units and zero placeholders."""
from __future__ import annotations

from app.connectors import DuckDBConnector
from app.semantic.loader import SemanticRegistry
from app.semantic.models import SemanticSchema
from app.services.sql_checks import check_sql, describe
from tests.test_query_graph import _load_registry

REGISTRY = _load_registry()
COLUMNS = {t: {c["name"] for c in cs} for t, cs in DuckDBConnector().get_schema().tables.items()}

PAID = "FROM order_payments op JOIN orders o ON o.order_id = op.order_id"


def test_a_sum_repeated_across_order_lines_goes_back_to_the_model():
    sql = (f"SELECT p.product_category_name_english, SUM(op.payment_value) {PAID} "
           "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY 1")
    verdict = check_sql(sql, REGISTRY, COLUMNS)
    assert verdict.reason == "fan_out" and "order_items" in verdict.repair


def test_the_lines_aggregated_per_order_first_pass():
    sql = (f"SELECT SUM(op.payment_value) {PAID} JOIN (SELECT order_id, COUNT(*) AS n FROM order_items "
           "GROUP BY order_id) lines ON lines.order_id = o.order_id "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    assert check_sql(sql, REGISTRY, COLUMNS).repair is None


def test_counting_distinct_keys_across_a_join_is_not_double_counting():
    sql = ("SELECT p.product_category_name_english, COUNT(DISTINCT o.order_id) FROM orders o "
           "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id GROUP BY 1")
    assert check_sql(sql, REGISTRY, COLUMNS).repair is None


def test_a_metric_without_its_agreed_condition_is_repaired_unless_the_question_overrides_it():
    sql = "SELECT SUM(op.payment_value) FROM order_payments op"
    verdict = check_sql(sql, REGISTRY, COLUMNS)
    assert verdict.reason == "policy" and "order_status NOT IN" in verdict.repair
    overridden = check_sql(sql, REGISTRY, COLUMNS, overridden={"total_revenue"})
    assert overridden.repair is None and overridden.notes and "Not applied" in overridden.policies[0]["statement"]


def test_the_agreed_condition_under_another_alias_counts():
    sql = ("SELECT SUM(pay.payment_value) FROM order_payments pay JOIN orders ord ON ord.order_id = pay.order_id "
           "WHERE ord.order_status NOT IN ('canceled', 'unavailable')")
    verdict = check_sql(sql, REGISTRY, COLUMNS)
    assert verdict.repair is None
    assert [m["name"] for m in verdict.measures] == ["total_revenue"]
    assert verdict.policies[0]["name"] == "total_revenue"


def test_the_description_reads_conditions_grouping_and_limit_from_the_sql():
    sql = ("SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS n FROM orders o JOIN customers c "
           "ON c.customer_id = o.customer_id WHERE o.order_status = 'delivered' GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    described = describe(sql, REGISTRY, COLUMNS)
    assert described["filters"] == [{"dimension": "orders.order_status", "label": "Order Status", "operator": "eq",
                                     "value": "delivered"}]
    assert described["grouping"] == [{"name": "customers.customer_state", "label": "Customer State"}]
    assert described["limit"] == 5


# A restaurant file: costs in several currencies, and ratings where 0 may mean "not rated".
RESTAURANTS = SemanticRegistry(SemanticSchema.model_validate({
    "version": "1", "dataset": "r", "description": "",
    "tables": [{"name": "restaurants"}], "joins": [],
    "metrics": [
        {"name": "average_cost", "display_name": "Average Cost", "description": "", "aggregation": "AVG",
         "sql_expression": "restaurants.cost", "required_tables": ["restaurants"], "unit_dimension": "currency"},
        {"name": "average_rating", "display_name": "Average Rating", "description": "", "aggregation": "AVG",
         "sql_expression": "restaurants.rating", "required_tables": ["restaurants"]},
        {"name": "average_rating_rated", "display_name": "Average Rating (rated)", "description": "",
         "aggregation": "AVG", "sql_expression": "restaurants.rating", "required_tables": ["restaurants"],
         "base_filter": "restaurants.rating <> 0", "excludes_zeros_of": "average_rating", "zero_share": 0.2},
    ],
    "dimensions": [
        {"name": "currency", "display_name": "Currency", "sql_expression": "restaurants.currency",
         "required_tables": ["restaurants"]},
        {"name": "country", "display_name": "Country", "sql_expression": "restaurants.country",
         "required_tables": ["restaurants"]},
    ],
    "time_dimensions": [],
}))
RESTAURANT_COLUMNS = {"restaurants": {"cost", "rating", "currency", "country"}}


def _units(per_group: list[int]):
    seen = []

    def run(sql: str) -> list[dict]:
        seen.append(sql)
        if "AS units" in sql:
            return [{"units": n} for n in per_group]
        return [{"currency": "INR", "n": 9}, {"currency": "USD", "n": 4}]

    return run, seen


def test_one_average_across_currencies_asks_which():
    run, seen = _units([12])
    verdict = check_sql("SELECT AVG(r.cost) FROM restaurants r", RESTAURANTS, RESTAURANT_COLUMNS, run=run)
    assert verdict.clarification["reason"] == "mixed_units"
    assert [c["label"] for c in verdict.clarification["choices"]] == ["Break down by Currency", "INR", "USD"]
    assert "COUNT(DISTINCT r.currency)" in seen[0]


def test_an_average_per_country_with_one_currency_each_passes_with_a_note():
    run, seen = _units([1, 1, 1])
    verdict = check_sql("SELECT r.country, AVG(r.cost) FROM restaurants r GROUP BY 1", RESTAURANTS,
                        RESTAURANT_COLUMNS, run=run)
    assert verdict.clarification is None and "its own Currency" in verdict.notes[0]
    assert "GROUP BY r.country" in seen[0]


def test_a_currency_named_in_the_query_settles_it():
    run, seen = _units([12])
    verdict = check_sql("SELECT AVG(r.cost) FROM restaurants r WHERE r.currency = 'INR'", RESTAURANTS,
                        RESTAURANT_COLUMNS, run=run)
    assert verdict.clarification is None and verdict.notes == ["Amounts are in INR."]
    assert not any("AS units" in probe for probe in seen)  # no unit probe; the value probe found INR
    chose_inr = [{"reason": "mixed_units", "constraint": {"kind": "filter", "value": "INR"}}]
    unfiltered = check_sql("SELECT AVG(r.cost) FROM restaurants r", RESTAURANTS, RESTAURANT_COLUMNS, run=run,
                           decisions=chose_inr)
    assert unfiltered.reason == "mixed_units"  # the choice is enforced, not the check skipped
    assert "restaurants.currency = 'INR'" in unfiltered.repair
    assert check_sql("SELECT AVG(r.cost) FROM restaurants r WHERE r.currency = 'INR'", RESTAURANTS,
                     RESTAURANT_COLUMNS, run=run, decisions=chose_inr).repair is None


def test_a_failed_unit_probe_asks_rather_than_answers():
    def broken(sql):
        raise RuntimeError("engine down")

    verdict = check_sql("SELECT AVG(r.cost) FROM restaurants r", RESTAURANTS, RESTAURANT_COLUMNS, run=broken)
    assert verdict.clarification["reason"] == "mixed_units"
    assert [c["id"] for c in verdict.clarification["choices"]] == ["by"]


def test_an_average_over_possible_zero_placeholders_asks_unless_the_sql_says():
    verdict = check_sql("SELECT AVG(rating) FROM restaurants", RESTAURANTS, RESTAURANT_COLUMNS)
    assert verdict.clarification["reason"] == "zero_placeholder"
    assert check_sql("SELECT AVG(rating) FROM restaurants WHERE rating <> 0", RESTAURANTS,
                     RESTAURANT_COLUMNS).clarification is None
    exclude = [{"reason": "zero_placeholder", "constraint": {"kind": "exclude"}}]
    keep = [{"reason": "zero_placeholder", "constraint": {"kind": "keep"}}]
    assert check_sql("SELECT AVG(rating) FROM restaurants", RESTAURANTS, RESTAURANT_COLUMNS,
                     decisions=exclude).reason == "zero_placeholder"
    assert check_sql("SELECT AVG(rating) FROM restaurants", RESTAURANTS, RESTAURANT_COLUMNS, decisions=keep).repair is None
    assert check_sql("SELECT AVG(rating) FROM restaurants WHERE rating > 0", RESTAURANTS, RESTAURANT_COLUMNS,
                     decisions=keep).reason == "zero_placeholder"


def test_sql_that_cannot_be_analysed_is_sent_back():
    assert check_sql("SELECT FROM WHERE (", REGISTRY, COLUMNS).reason == "unchecked"


def test_a_not_null_condition_on_the_aggregated_column_is_implied():
    sql = "SELECT AVG(r.review_score) FROM order_reviews r"
    verdict = check_sql(sql, REGISTRY, COLUMNS)
    assert verdict.repair is None and [m["name"] for m in verdict.measures] == ["average_review_score"]


def test_a_join_that_could_repeat_rows_passes_when_the_data_shows_the_conditions_keep_one():
    sql = (f"SELECT p.product_category_name_english, SUM(op.payment_value) {PAID} "
           "JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable') AND oi.order_item_id = 1 GROUP BY 1")
    seen = []

    def none_repeated(probe):
        seen.append(probe)
        return []

    verdict = check_sql(sql, REGISTRY, COLUMNS, run=none_repeated)
    assert verdict.repair is None and "checked on the data" in verdict.notes[0]
    assert "HAVING COUNT(*) > 1" in seen[0] and "oi.order_item_id = 1" in seen[0]
    assert check_sql(sql, REGISTRY, COLUMNS, run=lambda probe: [{"copies": 2}]).reason == "fan_out"


# Review of the checks, 2026-10-18: each finding as a regression test.
def test_a_cte_does_not_hide_a_column_from_the_definition_check():
    sql = "WITH p AS (SELECT payment_value FROM order_payments) SELECT SUM(payment_value) FROM p"
    assert check_sql(sql, REGISTRY, COLUMNS).reason == "policy"
    star = "WITH p AS (SELECT * FROM order_payments) SELECT SUM(p.payment_value) FROM p"
    assert check_sql(star, REGISTRY, COLUMNS).reason == "policy"
    kept = ("WITH valid AS (SELECT op.payment_value FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
            "WHERE o.order_status NOT IN ('canceled', 'unavailable')) SELECT SUM(v.payment_value) FROM valid v")
    assert check_sql(kept, REGISTRY, COLUMNS).repair is None


def test_a_column_that_cannot_be_traced_is_sent_back():
    union = ("SELECT SUM(x.v) FROM (SELECT payment_value AS v FROM order_payments UNION ALL "
             "SELECT price AS v FROM order_items) x")
    assert check_sql(union, REGISTRY, COLUMNS).reason == "unchecked"


def test_an_aggregate_over_a_subquery_aggregate_is_checked_where_that_aggregate_is():
    per_order = ("SELECT AVG(t.total) FROM (SELECT op.order_id, SUM(op.payment_value) AS total FROM order_payments op "
                 "JOIN orders o ON o.order_id = op.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') "
                 "GROUP BY op.order_id) t")
    assert check_sql(per_order, REGISTRY, COLUMNS).repair is None
    unfiltered = "SELECT AVG(t.total) FROM (SELECT order_id, SUM(payment_value) AS total FROM order_payments GROUP BY 1) t"
    assert check_sql(unfiltered, REGISTRY, COLUMNS).reason == "policy"


def test_sum_of_distinct_amounts_is_refused():
    verdict = check_sql("SELECT SUM(DISTINCT payment_value) FROM order_payments", REGISTRY, COLUMNS)
    assert verdict.reason == "distinct_amounts"


def test_condition_values_keep_their_case():
    sql = ("SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
           "WHERE o.order_status NOT IN ('CANCELED', 'UNAVAILABLE')")
    assert check_sql(sql, REGISTRY, COLUMNS).reason == "policy"


def test_a_condition_in_an_inner_join_counts():
    sql = ("SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
           "AND o.order_status NOT IN ('canceled', 'unavailable')")
    assert check_sql(sql, REGISTRY, COLUMNS).repair is None


def test_only_nullif_of_zero_on_the_column_leaves_zeros_out():
    assert check_sql("SELECT AVG(NULLIF(rating, 1)) FROM restaurants", RESTAURANTS,
                     RESTAURANT_COLUMNS).reason == "zero_placeholder"
    assert check_sql("SELECT AVG(NULLIF(rating, 0)) FROM restaurants", RESTAURANTS,
                     RESTAURANT_COLUMNS).clarification is None


def test_a_metric_is_named_only_when_the_expression_is_its_definition():
    scaled = ("SELECT SUM(op.payment_value * 100) FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
              "WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    verdict = check_sql(scaled, REGISTRY, COLUMNS)
    assert verdict.repair is None and verdict.measures == [] and "not its definition" in verdict.notes[0]
    assert check_sql("SELECT SUM(payment_value * 100) FROM order_payments", REGISTRY, COLUMNS).reason == "policy"
    swapped = ("SELECT SUM(oi.freight_value + oi.price) FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
               "WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    assert [m["name"] for m in check_sql(swapped, REGISTRY, COLUMNS).measures] == ["item_sales"]


class _Index:
    """The value index's interface the check uses: dimensions and snap."""

    dimensions = ["customer_state", "order_status"]

    def snap(self, dimension, raw):
        from app.grounding.values import SnapResult

        if raw.lower() == "sp" or raw == "Sao Paulo":
            return SnapResult(value="SP", confidence="exact")
        if raw == "Rio":
            return SnapResult(value=None, confidence="weak", alternatives=["RJ", "RN", "RS"])
        return SnapResult(value=None, confidence="none")


STATE = "SELECT COUNT(DISTINCT o.order_id) FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE "


def test_a_value_spelled_differently_from_the_data_goes_back_with_the_stored_value():
    verdict = check_sql(STATE + "c.customer_state = 'Sao Paulo'", REGISTRY, COLUMNS, values=_Index())
    assert verdict.reason == "value" and "'SP'" in verdict.repair
    assert check_sql(STATE + "c.customer_state = 'SP'", REGISTRY, COLUMNS, values=_Index()).repair is None
    assert check_sql(STATE + "c.customer_state = 'Atlantis'", REGISTRY, COLUMNS, values=_Index()).repair is None


def test_a_value_close_to_several_asks_which_and_the_answer_is_enforced():
    verdict = check_sql(STATE + "c.customer_state IN ('Rio')", REGISTRY, COLUMNS, values=_Index())
    choice = verdict.clarification
    assert choice["reason"] == "no_match" and choice["raw"] == "Rio"
    assert [c["label"] for c in choice["choices"]] == ["RJ", "RN", "RS", "Any Customer State"]
    assert choice["prompt"].startswith("Which customer state did you mean?")
    decided = [{"reason": "no_match", "constraint": choice["choices"][1]["constraint"]}]
    again = check_sql(STATE + "c.customer_state = 'Rio'", REGISTRY, COLUMNS, values=_Index(), decisions=decided)
    assert again.reason == "no_match" and "'RN'" in again.repair
    assert check_sql(STATE + "c.customer_state = 'RN'", REGISTRY, COLUMNS, values=_Index(),
                     decisions=decided).repair is None


def test_a_value_no_row_has_on_a_column_outside_the_index_asks_among_values_containing_it():
    sql = STATE.replace("WHERE ", "WHERE c.customer_city = 'Rio'")
    probes = []

    def run(probe):
        probes.append(probe)
        if "AS hit" in probe:
            return []
        return [{"value": "rio de janeiro", "n": 6882}, {"value": "rio grande", "n": 52}]

    verdict = check_sql(sql, REGISTRY, COLUMNS, run=run, values=_Index())
    assert [c["label"] for c in verdict.clarification["choices"]] == ["rio de janeiro", "rio grande", "Any Customer City"]
    assert "LOWER(customers.customer_city) LIKE '%rio%'" in probes[1]
    found = check_sql(sql, REGISTRY, COLUMNS, run=lambda probe: [{"hit": 1}], values=_Index())
    assert found.repair is None and found.clarification is None


# What the round-two fixes must still allow.
def test_a_table_joined_twice_without_a_definition_on_it_passes():
    sql = ("SELECT c.customer_state AS buyer, s.seller_state AS seller, SUM(oi.price) FROM order_items oi "
           "JOIN orders o ON o.order_id = oi.order_id JOIN customers c ON c.customer_id = o.customer_id "
           "JOIN sellers s ON s.seller_id = oi.seller_id JOIN customers c2 ON c2.customer_id = o.customer_id "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable') GROUP BY 1, 2")
    assert check_sql(sql, REGISTRY, COLUMNS).reason != "fan_out"


def test_comma_joins_linked_in_where_pass_and_unlinked_ones_do_not():
    linked = ("SELECT SUM(op.payment_value) FROM order_payments op, orders o WHERE o.order_id = op.order_id "
              "AND o.order_status NOT IN ('canceled', 'unavailable')")
    assert check_sql(linked, REGISTRY, COLUMNS).repair is None
    crossed = ("SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
               "CROSS JOIN customers c WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    assert "does not link" in check_sql(crossed, REGISTRY, COLUMNS).repair


def test_a_rejoined_aggregate_passes_when_the_data_shows_no_repeats():
    sql = ("WITH p AS (SELECT op.order_id, SUM(op.payment_value) AS amount FROM order_payments op "
           "JOIN orders o ON o.order_id = op.order_id WHERE o.order_status NOT IN ('canceled', 'unavailable') "
           "GROUP BY op.order_id) SELECT SUM(p.amount) FROM p JOIN orders o2 ON o2.order_id = p.order_id")
    assert check_sql(sql, REGISTRY, COLUMNS, run=lambda probe: []).repair is None
    assert check_sql(sql, REGISTRY, COLUMNS).reason == "fan_out"  # no data to show it: sent back


def test_the_panel_says_when_part_of_the_query_is_not_listed():
    from app.services.sql_checks import describe

    assert describe("WITH p AS (SELECT 1 AS x) SELECT x FROM p", REGISTRY, COLUMNS)["partial"] is True
    assert describe("SELECT c.customer_state FROM customers c", REGISTRY, COLUMNS)["partial"] is False


# What the V1-V5 fixes must still allow.
RN = [{"reason": "no_match", "constraint": {"kind": "value", "column": "customers.customer_state", "raw": "Rio",
                                             "value": "RN"}}]


def test_a_chosen_value_in_a_correlated_exists_or_an_in_subquery_counts():
    exists = ("SELECT COUNT(DISTINCT o.order_id) FROM orders o WHERE EXISTS (SELECT 1 FROM customers c "
              "WHERE c.customer_id = o.customer_id AND c.customer_state = 'RN')")
    assert check_sql(exists, REGISTRY, COLUMNS, values=_Index(), decisions=RN).repair is None
    within = ("SELECT COUNT(DISTINCT o.order_id) FROM orders o WHERE o.customer_id IN "
              "(SELECT c.customer_id FROM customers c WHERE c.customer_state = 'RN')")
    assert check_sql(within, REGISTRY, COLUMNS, values=_Index(), decisions=RN).repair is None


def test_a_join_on_keys_the_model_does_not_declare_is_shown_on_the_data():
    sql = ("SELECT SUM(op.payment_value) FROM order_payments op JOIN orders o ON o.order_id = op.order_id "
           "JOIN customers c ON c.customer_unique_id = o.customer_id "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable')")
    assert check_sql(sql, REGISTRY, COLUMNS, run=lambda probe: []).repair is None
    assert "join keys" in check_sql(sql, REGISTRY, COLUMNS).repair  # no data to show it


def test_a_one_row_total_joined_for_a_share_passes():
    sql = ("SELECT r.country, AVG(r.cost) / MAX(t.overall) FROM restaurants r "
           "CROSS JOIN (SELECT AVG(cost) AS overall FROM restaurants WHERE currency = 'INR') t "
           "WHERE r.currency = 'INR' GROUP BY 1")
    assert check_sql(sql, RESTAURANTS, RESTAURANT_COLUMNS).reason != "fan_out"
