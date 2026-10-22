"""Regression tests for the code review of 2026-10-13 (B-numbers).

Answers are checked against figures worked out by hand on small DuckDB fixtures, not against the
compiler's own output, so a wrong definition cannot bless itself.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.agent.ambiguity import fan_out, find_ambiguity, mixed_units
from app.models import SemanticIntent
from app.semantic import SemanticRegistry, build_sql_from_intent, load_semantic_registry

DEMO = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"


@pytest.fixture(scope="module")
def registry():
    return load_semantic_registry(DEMO)


def _shop() -> duckdb.DuckDBPyConnection:
    """o1: delivered, two items in 'toys', paid 60 + 40 (100). o2: canceled, one 'toys' item, paid 30.
    o3: delivered at noon on 2018-12-31, one 'books' item, paid 20."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, order_status VARCHAR, "
                 "order_purchase_timestamp TIMESTAMP, order_delivered_customer_date TIMESTAMP)")
    conn.execute("INSERT INTO orders VALUES "
                 "('o1','c1','delivered','2018-06-01 10:00','2018-06-05 10:00'), "
                 "('o2','c1','canceled','2018-07-01 10:00',NULL), "
                 "('o3','c2','delivered','2018-12-31 12:00','2019-01-03 12:00')")
    conn.execute("CREATE TABLE order_items (order_id VARCHAR, product_id VARCHAR, seller_id VARCHAR, "
                 "price DOUBLE, freight_value DOUBLE)")
    conn.execute("INSERT INTO order_items VALUES ('o1','p1','s1',45,5), ('o1','p1','s1',45,5), "
                 "('o2','p1','s1',25,5), ('o3','p2','s1',15,5)")
    conn.execute("CREATE TABLE order_payments (order_id VARCHAR, payment_type VARCHAR, payment_value DOUBLE)")
    conn.execute("INSERT INTO order_payments VALUES ('o1','credit_card',60), ('o1','voucher',40), "
                 "('o2','credit_card',30), ('o3','boleto',20)")
    conn.execute("CREATE TABLE products (product_id VARCHAR, product_category_name_english VARCHAR, "
                 "product_category_name VARCHAR)")
    conn.execute("INSERT INTO products VALUES ('p1','toys','brinquedos'), ('p2','books','livros')")
    return conn


def _run(registry, intent: SemanticIntent):
    return _shop().execute(build_sql_from_intent(intent, registry)).fetchall()


# B8: the data spells it "canceled"; the demo checked "cancelled", so nothing was ever excluded or counted.
def test_canceled_orders_are_excluded_from_revenue_and_counted_as_cancellations(registry):
    assert _run(registry, SemanticIntent(metric="total_revenue")) == [(120.0,)]  # 100 + 20, not o2's 30
    rate = _run(registry, SemanticIntent(metric="cancellation_rate"))[0][0]
    assert rate == pytest.approx(1 / 3)


# B3: payments are per order; by category they were counted once per item.
def test_revenue_by_category_is_not_multiplied_across_items(registry):
    by_category = SemanticIntent(metric="total_revenue", dimensions=["product_category"])
    found = fan_out(by_category, registry, lambda intent: [{"metric_value": 1.0}])
    assert found["kind"] == "substitute" and found["patch"] == {"set_metric": "item_sales"}
    # Different totals on real data (vouchers, interest): ask instead of substituting.
    totals = iter([{"metric_value": 120.0}, {"metric_value": 130.0}])
    assert fan_out(by_category, registry, lambda intent: [next(totals)])["kind"] == "choice"
    rows = dict(_run(registry, SemanticIntent(metric="item_sales", dimensions=["product_category"])))
    assert rows == {"toys": 100.0, "books": 20.0}  # o2 canceled; the payment sum by category gave toys 200


# Average order value was the mean payment row: o1 paid 60 + 40 gave 50, not 100.
def test_average_order_value_is_payments_per_order(registry):
    assert _run(registry, SemanticIntent(metric="average_order_value"))[0][0] == pytest.approx(60.0)  # (100+20)/2
    # And it is a ratio with a SUM inside: by category it would be inflated, so it is checked too.
    assert fan_out(SemanticIntent(metric="average_order_value", dimensions=["product_category"]),
                   registry, lambda intent: []) is not None


def test_a_cancellation_rate_by_category_counts_each_order_once(registry):
    assert fan_out(SemanticIntent(metric="cancellation_rate", dimensions=["product_category"]),
                   registry, lambda intent: []) is None
    rows = dict(_run(registry, SemanticIntent(metric="cancellation_rate", dimensions=["product_category"])))
    assert rows == {"toys": pytest.approx(0.5), "books": 0.0}  # o1 (two toys items) once, o2 canceled


def test_an_average_across_a_fan_out_is_answered_with_a_note(registry):
    found = fan_out(SemanticIntent(metric="average_delivery_days", dimensions=["seller_state"]),
                    registry, lambda intent: [])
    assert found["kind"] == "note" and "averaged per orders" in found["text"]


# B5: an end date is a whole day, the column a timestamp.
def test_the_last_day_of_a_range_is_included(registry):
    in_2018 = SemanticIntent(metric="order_count", start_date=date(2018, 1, 1), end_date=date(2018, 12, 31))
    sql = build_sql_from_intent(in_2018, registry)
    assert "< '2019-01-01'" in sql and "<= '2018-12-31'" not in sql
    assert _run(registry, in_2018) == [(3,)]  # o3 at noon on 2018-12-31 counts
    one_day = SemanticIntent(metric="order_count", start_date=date(2018, 12, 31), end_date=date(2018, 12, 31))
    assert _run(registry, one_day) == [(1,)]


def test_day_conditions_on_columns_cover_the_whole_day():
    from app.models.semantic_intent import FilterCondition
    from app.semantic.sql_builder import _build_filter_clause

    def clause(operator, value):
        return _build_filter_clause(FilterCondition(dimension="d", operator=operator, value=value), "t.d")

    assert clause("lte", "2018-12-31") == "t.d < '2019-01-01'"
    assert clause("gt", "2018-12-31") == "t.d >= '2019-01-01'"
    assert clause("between", ["2018-01-01", "2018-12-31"]) == "t.d >= '2018-01-01' AND t.d < '2019-01-01'"
    assert clause("lte", "2020-02-28") == "t.d < '2020-02-29'"  # leap year
    assert clause("lte", 300000) == "t.d <= 300000"  # numbers are untouched
    assert clause("lte", "abc") == "t.d <= 'abc'"


# B18: generated ratios are row values; divided as they were, each row gave 1 or 0.
def test_a_ratio_of_row_values_is_summed_before_dividing():
    from app.semantic.models import SemanticMetric, SemanticSchema, SemanticTable

    ratio = SemanticMetric(name="cancel_share", display_name="Cancel share", description="d", aggregation="RATIO",
                           numerator_sql="CASE WHEN orders.order_status = 'canceled' THEN 1 ELSE 0 END",
                           denominator_sql="1", required_tables=["orders"])
    registry = SemanticRegistry(SemanticSchema(version="1", dataset="d", description="d",
                                               tables=[SemanticTable(name="orders")], joins=[], metrics=[ratio],
                                               dimensions=[], time_dimensions=[]))
    sql = build_sql_from_intent(SemanticIntent(metric="cancel_share"), registry)
    assert _shop().execute(sql).fetchall() == [(pytest.approx(1 / 3),)]


# B4: an empty answer for the period asked about stays empty.
def test_an_empty_period_is_not_answered_with_all_time_figures():
    from app.agent.nodes.validator import build_validator_node

    intent = SemanticIntent(metric="total_revenue", start_date=date(1999, 1, 1), end_date=date(1999, 12, 31))
    out = build_validator_node()({"intent": intent, "row_count": 0, "rows": [], "retry_count": 0})
    assert out["validation_status"] == "empty" and out["correction_hint"] is None


# B6: explicit intents get the same checks, and a check that cannot run does not pass.
def test_an_explicit_intent_is_checked_for_double_counting(registry):
    from test_agent_conversation import LLM_CONFIG
    from test_query_graph import StubConnector

    from app.agent import QueryGraphDependencies, QueryGraphRunner
    from app.services import IntentMapperRouter

    connector = StubConnector()
    runner = QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=registry,
        intent_mapper=IntentMapperRouter(config=LLM_CONFIG, value_index=lambda: None), intent_config=LLM_CONFIG))
    done = runner.invoke(question="x", query_id="q", explicit_intent=SemanticIntent(
        metric="average_order_value", dimensions=["product_category"]))
    assert done.get("unsupported") or done.get("clarification")
    assert not any("GROUP BY p.product_category_name_english" in sql and "SUM(op.payment_value)" in sql
                   for sql in connector.executed_sql)


def _with_unit(registry):
    metrics = [m.model_copy(update={"unit_dimension": "payment_type"}) if m.name == "total_revenue" else m
               for m in registry.schema.metrics]
    return SemanticRegistry(registry.schema.model_copy(update={"metrics": metrics}))


def test_a_failed_or_truncated_unit_probe_asks_instead_of_passing(registry):
    unit_registry = _with_unit(registry)

    def failing(intent):
        raise RuntimeError("engine hiccup")

    asked = mixed_units(SemanticIntent(metric="total_revenue"), unit_registry, failing)
    assert asked["kind"] == "choice" and asked["choices"][0]["patch"] == {"add_dimension": "payment_type"}
    full = [{"payment_type": "boleto", "metric_value": 1.0}] * 5000
    assert mixed_units(SemanticIntent(metric="total_revenue"), unit_registry, lambda i: full)["kind"] == "choice"


def test_a_check_that_crashes_refuses_but_no_probe_skips(registry, monkeypatch):
    import app.agent.ambiguity as ambiguity

    def broken(intent, registry, probe):
        raise KeyError("bug")

    monkeypatch.setattr(ambiguity, "CHECKS", (broken,))
    pending, _, _, _ = find_ambiguity(SemanticIntent(metric="order_count"), registry, lambda i: [])
    assert pending["kind"] == "refuse"
    monkeypatch.setattr(ambiguity, "CHECKS", (ambiguity.mixed_units,))
    pending, _, _, _ = find_ambiguity(SemanticIntent(metric="total_revenue"), _with_unit(registry), None)
    assert pending is None  # offline: nothing to check with


# B9: a metric whose condition does not resolve is dropped, not widened.
def test_a_metric_with_an_unresolvable_condition_is_dropped():
    from app.connectors import DuckDBConnector
    from app.services.schema_completion import SchemaProposal, complete_schema

    proposal = SchemaProposal.model_validate({"dataset": "Olist", "dimensions": [], "time_dimensions": [], "metrics": [
        {"name": "paid_revenue", "aggregation": "SUM", "sql_expression": "order_payments.payment_value",
         "base_filter": "orders.missing_status = 'paid'"}]})
    schema, notes = complete_schema(proposal, DuckDBConnector().get_schema())
    assert "paid_revenue" not in {m.name for m in schema.metrics}
    assert any("paid_revenue" in n and "does not resolve" in n for n in notes)


# B10: zeros are "not recorded" only on a scale without a zero.
def test_zeros_in_a_discount_are_real_values():
    from test_schema_completion import _ProfileConnector, _proposal, _zomato_ctx

    from app.services.schema_completion import complete_schema
    from app.services.schema_generator import exclude_placeholder_zeros

    schema, _ = complete_schema(_proposal(metrics=[
        {"name": "avg_votes", "aggregation": "AVG", "sql_expression": "zomato.votes"}]), _zomato_ctx())
    connector = _ProfileConnector({"votes": (100, 20, 10, 10)})  # 20 zeros, 80 tens: passes the old test
    metric = next(m for m in exclude_placeholder_zeros(schema, connector).metrics if m.name == "avg_votes")
    assert metric.sql_expression == "zomato.votes"


# B11: charts read the columns the compiler writes.
def test_a_multi_series_line_uses_the_time_bucket_in_time_order():
    from app.charts.templates import build_chart_spec

    intent = SemanticIntent(metric="total_revenue", dimensions=["payment_type"], time_dimension="order_date")
    rows = [{"payment_type": "boleto", "time_bucket": "2018-02-01", "metric_value": 5},
            {"payment_type": "boleto", "time_bucket": "2018-01-01", "metric_value": 9}]
    trace = build_chart_spec("multi_line", rows, intent, "t")["data"][0]
    assert list(trace["x"]) == ["2018-01-01", "2018-02-01"] and list(trace["y"]) == [9, 5]


def test_a_two_way_bar_keeps_both_breakdowns():
    from app.charts.templates import build_chart_spec

    intent = SemanticIntent(metric="order_count", dimensions=["customer_state", "payment_type"])
    rows = [{"customer_state": "SP", "payment_type": "boleto", "metric_value": 2},
            {"customer_state": "SP", "payment_type": "voucher", "metric_value": 3}]
    data = build_chart_spec("bar", rows, intent, "t")["data"]
    assert {d["name"] for d in data} == {"boleto", "voucher"}


# B14: the key checked is the one schema generation will use.
def test_schema_generation_checks_the_schema_task_provider_key(monkeypatch):
    from app.llm import missing_api_key
    from app.services.intent_mapper import IntentMapperConfig

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_PROVIDER_SCHEMA", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert missing_api_key(IntentMapperConfig.from_env().for_task("schema").provider) is None
    monkeypatch.delenv("OPENAI_API_KEY")
    assert missing_api_key(IntentMapperConfig.from_env().for_task("schema").provider) == "OPENAI_API_KEY"
    with pytest.raises(ValueError, match="Unknown LLM task"):
        IntentMapperConfig.from_env().for_task("shema")


# One reading of "top N" for both mapping paths (review: two rule sets with caps 500 and 100).
@pytest.mark.parametrize("question, limit, ascending", [
    ("top 5 states by revenue", 5, False),
    ("bottom 7 customers by revenue", 7, True),
    ("first 3 customers by revenue", 3, True),
    ("revenue in the first quarter by state", None, False),
    ("which is the top selling album", 1, False),
    ("top 500 customers", None, False),
    ("revenue over the last 12 months", None, False),
])
def test_ranking_is_read_the_same_way_everywhere(question, limit, ascending):
    from app.services.intent_mapper import ranking_request

    assert ranking_request(question) == type(ranking_request(""))(limit=limit, ascending=ascending)


def test_retrieved_examples_keep_the_intent_field_order():
    """Sorted keys put order_by before time_dimension, and the 4B model then sorted trends by value."""
    import pyarrow as pa

    from app.grounding.examples import ExampleIndex, examples_table

    example = {"question": "Quarterly orders trend", "intent": {
        "metric": "order_count", "dimensions": [], "filters": [], "time_dimension": "order_date",
        "time_granularity": "quarter", "start_date": None, "end_date": None, "order_by": "time_asc", "limit": 100}}
    table = examples_table([example])
    assert list(__import__("json").loads(table.column("intent")[0].as_py())) [0] == "dimensions"  # stored sorted
    index = ExampleIndex(client=None, source="s", corpus=table)
    keys = list(index._by_id["x0"].intent)
    assert keys.index("time_dimension") < keys.index("order_by") and keys[0] == "metric"


# Second review: an average across a one-to-many join is taken per "one" row, not per child row.
def test_an_average_across_items_counts_each_order_once(registry):
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id VARCHAR, order_status VARCHAR, order_purchase_timestamp TIMESTAMP, "
                 "order_delivered_customer_date TIMESTAMP)")
    conn.execute("INSERT INTO orders VALUES ('o1','delivered','2018-01-01','2018-01-03'), "
                 "('o2','delivered','2018-01-01','2018-01-11')")
    conn.execute("CREATE TABLE order_items (order_id VARCHAR, product_id VARCHAR, seller_id VARCHAR, price DOUBLE, "
                 "freight_value DOUBLE)")
    conn.execute("INSERT INTO order_items VALUES ('o1','p1','s1',1,0), ('o1','p1','s1',1,0), ('o1','p1','s1',1,0), "
                 "('o2','p1','s1',1,0)")
    conn.execute("CREATE TABLE products (product_id VARCHAR, product_category_name_english VARCHAR, "
                 "product_category_name VARCHAR)")
    conn.execute("INSERT INTO products VALUES ('p1','toys','brinquedos')")
    sql = build_sql_from_intent(SemanticIntent(metric="average_delivery_days", dimensions=["product_category"]),
                                registry)
    assert "AS _grain_0" in sql and "GROUP BY orders.order_id" in sql
    assert conn.execute(sql).fetchall() == [("toys", 6.0)]  # (2 + 10) / 2, not (2+2+2+10) / 4
    # No repeat, no subquery: the plain aggregate as before.
    assert "_grain_" not in build_sql_from_intent(SemanticIntent(metric="average_delivery_days",
                                                                  dimensions=["order_status"]), registry)


# Second review: whether 0 means "not recorded" is asked, not assumed.
def test_zeros_that_may_mean_not_recorded_are_asked_about(registry):
    from app.agent.ambiguity import ZERO_EXCLUDE, ZERO_INCLUDE, apply_choice, find_ambiguity
    from app.semantic.models import SemanticMetric

    base = next(m for m in registry.schema.metrics if m.name == "average_review_score")
    variant = base.model_copy(update={"name": "average_review_score_excluding_zero", "excludes_zeros_of": base.name,
                                      "zero_share": 0.2, "sql_expression": "NULLIF(r.review_score, 0)"})
    reg = SemanticRegistry(registry.schema.model_copy(update={"metrics": [*registry.schema.metrics, variant]}))
    intent = SemanticIntent(metric="average_review_score")
    pending, _, _, _ = find_ambiguity(intent, reg, None)
    assert pending["reason"] == "zero_placeholder" and "20% of rows" in pending["prompt"]
    assert apply_choice(intent, pending, ZERO_EXCLUDE)[0].metric == variant.name
    kept, _ = apply_choice(intent, pending, ZERO_INCLUDE)
    assert kept.metric == "average_review_score"
    # Answered this turn: not asked again for the unchanged intent.
    assert find_ambiguity(kept, reg, None, frozenset({"zero_placeholder"}))[0] is None
    assert isinstance(SemanticMetric, type)
