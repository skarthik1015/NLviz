from __future__ import annotations

from pathlib import Path

from app.agent.nodes.intent_mapper import _drop_redundant_breakdown, _filter_named_values
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import load_semantic_registry

REGISTRY = load_semantic_registry(Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml")


def _hint(dimension, value, exact=True):
    return {"kind": "hint", "dimension": dimension, "value": value, "exact": exact}


def test_a_named_value_turns_its_breakdown_into_a_filter():
    intent = SemanticIntent(metric="order_count", dimensions=["customer_state"])
    new, notes = _filter_named_values(intent, "How many orders came from SP?", REGISTRY, [_hint("customer_state", "SP")])
    assert new.dimensions == [] and new.filters == [FilterCondition(dimension="customer_state", operator="eq", value="SP")]
    assert notes


def test_a_breakdown_the_question_asks_for_is_kept():
    intent = SemanticIntent(metric="order_count", dimensions=["customer_state"])
    new, notes = _filter_named_values(intent, "Orders by customer state, is SP the largest?", REGISTRY,
                                      [_hint("customer_state", "SP")])
    assert new == intent and notes == []


def test_only_exact_hints_for_grouped_unfiltered_dimensions_change_anything():
    grouped = SemanticIntent(metric="order_count", dimensions=["customer_state"])
    # A retrieved (not word-for-word) value is not enough.
    assert _filter_named_values(grouped, "orders from sao paulo", REGISTRY, [_hint("customer_state", "SP", False)])[0] == grouped
    # A value whose dimension is not in the breakdown never adds a filter from nothing.
    plain = SemanticIntent(metric="order_count")
    assert _filter_named_values(plain, "Average orders", REGISTRY, [_hint("order_status", "delivered")])[0] == plain
    # Two values named for one dimension: a comparison, left to the LLM.
    assert _filter_named_values(grouped, "SP vs RJ orders", REGISTRY,
                                [_hint("customer_state", "SP"), _hint("customer_state", "RJ")])[0] == grouped


def test_contains_filter_on_a_grouped_dimension_drops_the_breakdown():
    intent = SemanticIntent(metric="order_count", dimensions=["product_category"],
                            filters=[FilterCondition(dimension="product_category", operator="contains", value="furniture")])
    new, _ = _drop_redundant_breakdown(intent, "How many furniture orders?", REGISTRY, set())
    assert new.dimensions == [] and len(new.filters) == 1


class _Flags:
    """Value index stand-in: order_status pretends to be a yes/no column."""

    def __init__(self, values):
        self._values = values

    def values(self, dimension):
        return self._values.get(dimension, set())


def test_a_yes_no_the_question_states_becomes_a_filter():
    from app.agent.nodes.intent_mapper import _flag_filters

    index = _Flags({"order_status": {"Yes", "No"}})
    grouped = SemanticIntent(metric="order_count", dimensions=["order_status"])
    yes, notes = _flag_filters(grouped, "How many orders offer table booking?", REGISTRY, index)
    assert yes.dimensions == [] and yes.filters == [FilterCondition(dimension="order_status", operator="eq", value="Yes")]
    no, _ = _flag_filters(grouped, "How many orders don't offer table booking?", REGISTRY, index)
    assert no.filters[0].value == "No"
    for asks_split in ("Orders with and without table booking", "Share of orders with booking",
                       "Orders by order status", "Booking vs no booking"):
        assert _flag_filters(grouped, asks_split, REGISTRY, index)[0] == grouped, asks_split
    many = _Flags({"order_status": {"delivered", "canceled", "shipped"}})
    assert _flag_filters(grouped, "How many delivered orders?", REGISTRY, many)[0] == grouped
    assert _flag_filters(grouped, "anything", REGISTRY, None)[0] == grouped


def test_an_explicit_top_n_sets_the_limit_and_lowest_sorts_ascending():
    from app.agent.nodes.intent_mapper import _requested_ranking

    intent = SemanticIntent(metric="order_count", dimensions=["customer_state"], limit=5000)
    assert _requested_ranking(intent, "Which 5 states have the most orders?")[0].limit == 5
    assert _requested_ranking(intent, "Top 10 states by orders")[0].limit == 10
    lowest = _requested_ranking(intent, "Which 3 states have the fewest orders?")[0]
    assert lowest.limit == 3 and lowest.order_by == "metric_asc"
    assert _requested_ranking(intent, "Orders by state in the top 3 months")[0].limit == 5000  # time words
    over_time = intent.model_copy(update={"time_dimension": "order_date", "time_granularity": "month"})
    assert _requested_ranking(over_time, "Top 5 months")[0] == over_time
    assert _requested_ranking(SemanticIntent(metric="order_count"), "Top 5")[1] == []
    # "Which is the top ..." asks for one, by name (Chinook: "which is the top selling album" listed 100).
    assert _requested_ranking(intent, "Which is the top selling state?")[0].limit == 1
    one_lowest = _requested_ranking(intent, "What is the least ordered state?")[0]
    assert one_lowest.limit == 1 and one_lowest.order_by == "metric_asc"
    assert _requested_ranking(intent, "Which states sell the most?")[0].limit == 5000      # plural: all, ranked


def test_several_items_of_a_list_dimension_are_one_group_not_a_breakdown():
    from test_list_dimensions import _registry

    registry = _registry()
    intent = SemanticIntent(metric="restaurants", dimensions=["cuisines"],
                            filters=[FilterCondition(dimension="cuisines", operator="in", value=["Japanese", "Sushi"])])
    new, _ = _drop_redundant_breakdown(intent, "How many restaurants serve Japanese or Sushi?", registry, set())
    assert new.dimensions == [] and new.filters == intent.filters
    kept, _ = _drop_redundant_breakdown(intent, "Japanese and Sushi restaurants by cuisine", registry, set())
    assert kept.dimensions == ["cuisines"]


def test_plural_and_singular_forms_match_values_word_for_word():
    from app.grounding.values import _plural_forms, _singular_forms

    assert "cafes" in _plural_forms("cafe") and "bakeries" in _plural_forms("bakery")
    assert _plural_forms("sp") == ("sp",)
    assert "cafe" in _singular_forms("cafes") and "bakery" in _singular_forms("bakeries")


def test_an_aggregation_word_that_only_names_the_metric_is_not_a_filter_value():
    from app.agent.nodes.intent_mapper import _aggregation_word_filters

    average = FilterCondition(dimension="order_status", operator="eq", value="Average")
    intent = SemanticIntent(metric="average_review_score",
                            filters=[average, FilterCondition(dimension="customer_state", operator="eq", value="SP")])
    new, notes = _aggregation_word_filters(intent, "Average review score of orders from SP", REGISTRY)
    assert new.filters == [FilterCondition(dimension="customer_state", operator="eq", value="SP")] and notes
    # The word stands on its own: it is a value the question asks about.
    kept = intent.model_copy(update={"filters": [average]})
    assert _aggregation_word_filters(kept, "Average review score of orders rated Average", REGISTRY)[0] == kept
    assert _aggregation_word_filters(kept, "How many orders are Average?", REGISTRY)[0] == kept


def test_per_entity_with_an_average_is_the_unit_not_a_breakdown_by_name():
    from app.agent.nodes.intent_mapper import _per_entity_average
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable

    def dim(name, cardinality):
        return SemanticDimension(name=name, display_name=name.replace("_", " ").title(), sql_expression=f"z.{name}",
                                 required_tables=["z"], cardinality=cardinality)

    def metric(name, aggregation):
        return SemanticMetric(name=name, display_name=name, description=name, aggregation=aggregation,
                              sql_expression="z.votes", required_tables=["z"])

    registry = SemanticRegistry(SemanticSchema(
        version="1.0", dataset="z", description="d", tables=[SemanticTable(name="z")], joins=[],
        metrics=[metric("average_votes", "AVG"), metric("total_votes", "SUM")],
        dimensions=[dim("restaurant_name", "high"), dim("rating_text", "low")], time_dimensions=[]))
    intent = SemanticIntent(metric="average_votes", dimensions=["rating_text", "restaurant_name"])
    new, notes = _per_entity_average(intent, "Average votes per restaurant by rating text", registry)
    assert new.dimensions == ["rating_text"] and notes
    # Asked for by name, or not an average: kept.
    assert _per_entity_average(intent, "Average votes by restaurant name and rating text", registry)[0] == intent
    total = intent.model_copy(update={"metric": "total_votes"})
    assert _per_entity_average(total, "Total votes per restaurant by rating text", registry)[0] == total


def _invoice_registry():
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable

    def dim(name, table, cardinality="medium"):
        return SemanticDimension(name=name, display_name=name.replace("_", " ").title(), sql_expression=f"{table}.x",
                                 required_tables=[table], cardinality=cardinality)

    def metric(name, aggregation, table):
        return SemanticMetric(name=name, display_name=name.replace("_", " ").title(), description=name,
                              aggregation=aggregation, sql_expression=f"{table}.x", required_tables=[table])

    return SemanticRegistry(SemanticSchema(
        version="1.0", dataset="c", description="d", tables=[SemanticTable(name=t) for t in ("invoice", "customer", "invoice_line")],
        joins=[], metrics=[metric("total_invoice_amount", "SUM", "invoice"), metric("invoices", "COUNT", "invoice"),
                           metric("customers", "COUNT", "customer"), metric("total_quantity", "SUM", "invoice_line")],
        dimensions=[dim("billing_country", "invoice"), dim("customer_country", "customer"),
                    dim("invoice_line_unit_price", "invoice_line", "low"), dim("customer_state", "customer")],
        time_dimensions=[]))


def test_how_many_things_counts_them():
    from app.agent.nodes.intent_mapper import _how_many_counts

    registry = _invoice_registry()
    intent = SemanticIntent(metric="total_invoice_amount")
    new, notes = _how_many_counts(intent, "How many invoices came from Brazil?", registry)
    assert new.metric == "invoices" and notes
    # A count already, or no count named after the things: left alone.
    assert _how_many_counts(new, "How many invoices came from Brazil?", registry)[0] == new
    units = SemanticIntent(metric="total_quantity")
    assert _how_many_counts(units, "How many units were sold?", registry)[0] == units

    # Two counts mention invoices (Chinook on MySQL, run 8): the one named by the noun alone wins.
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticMetric

    lines = SemanticMetric(name="invoice_lines", display_name="Number of Invoice Lines", description="n",
                           aggregation="COUNT_DISTINCT", sql_expression="invoice_line.x", required_tables=["invoice_line"])
    both = SemanticRegistry(registry.schema.model_copy(update={"metrics": [*registry.schema.metrics, lines]}))
    assert _how_many_counts(intent, "How many invoices came from Brazil?", both)[0].metric == "invoices"


def test_a_breakdown_the_question_never_asks_for_is_dropped():
    from app.agent.nodes.intent_mapper import _unasked_breakdowns

    registry = _invoice_registry()
    intent = SemanticIntent(metric="invoices", dimensions=["invoice_line_unit_price"])
    new, notes = _unasked_breakdowns(intent, "How many invoices came from Brazil?", registry)
    assert new.dimensions == [] and notes
    # A cue word ("by region" for state), the dimension's own words, or carried over from the last turn: kept.
    region = SemanticIntent(metric="customers", dimensions=["customer_state"])
    assert _unasked_breakdowns(region, "Customers by region", registry)[0] == region
    assert _unasked_breakdowns(intent, "Invoices at each unit price", registry)[0] == intent
    assert _unasked_breakdowns(intent, "Invoice count for unit prices", registry)[0] == intent
    assert _unasked_breakdowns(intent, "Only March 2006", registry, ["invoice_line_unit_price"])[0] == intent


def test_a_value_named_in_two_dimensions_filters_the_one_on_the_metrics_table():
    from app.agent.nodes.intent_mapper import _filter_named_values

    registry = _invoice_registry()
    evidence = [{"kind": "hint", "exact": True, "dimension": d, "value": "Brazil"}
                for d in ("billing_country", "customer_country")]
    new, notes = _filter_named_values(SemanticIntent(metric="invoices"), "How many invoices came from Brazil?",
                                      registry, evidence)
    assert [(f.dimension, f.value) for f in new.filters] == [("billing_country", "Brazil")] and notes
    # Neither dimension on the metric's table: no guess.
    intent = SemanticIntent(metric="total_quantity")
    assert _filter_named_values(intent, "Units sold in Brazil", registry, evidence)[0] == intent


def test_by_a_table_means_its_rows_by_name():
    from app.agent.nodes.intent_mapper import _entity_name_breakdowns
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable

    def dim(name, sql):
        return SemanticDimension(name=name, display_name=name.replace("_", " ").title(), sql_expression=sql,
                                 required_tables=["employees"], cardinality="low")

    registry = SemanticRegistry(SemanticSchema(
        version="1.0", dataset="n", description="d", tables=[SemanticTable(name="employees")], joins=[],
        metrics=[SemanticMetric(name="sales", display_name="Sales", description="s", aggregation="SUM",
                                sql_expression="employees.x", required_tables=["employees"])],
        dimensions=[dim("employees_job_title", "employees.job_title"), dim("employee_city", "employees.city"),
                    dim("employees_name", "employees.first_name || ' ' || employees.last_name")],
        time_dimensions=[]))
    intent = SemanticIntent(metric="sales", dimensions=["employees_job_title"])
    new, notes = _entity_name_breakdowns(intent, "Sales by employee", registry)
    assert new.dimensions == ["employees_name"] and notes
    # The column named in the question, or no "by employee": kept.
    assert _entity_name_breakdowns(intent, "Sales by employee job title", registry)[0] == intent
    city = SemanticIntent(metric="sales", dimensions=["employee_city"])
    assert _entity_name_breakdowns(city, "Sales by city", registry)[0] == city


def test_an_average_nobody_asked_for_becomes_the_sum_of_the_same_column():
    from app.agent.nodes.intent_mapper import _unasked_average
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticMetric, SemanticSchema, SemanticTable

    def metric(name, aggregation, sql):
        return SemanticMetric(name=name, display_name=name, description=name, aggregation=aggregation,
                              sql_expression=sql, required_tables=[sql.split(".")[0]])

    registry = SemanticRegistry(SemanticSchema(
        version="1.0", dataset="p", description="d", tables=[SemanticTable(name="payment"), SemanticTable(name="z")],
        joins=[], dimensions=[], time_dimensions=[],
        metrics=[metric("average_payment_amount", "AVG", "payment.amount"),
                 metric("total_payment_amount", "SUM", "payment.amount"),
                 metric("average_cost_for_two", "AVG", "z.average_cost_for_two"),
                 metric("total_average_cost_for_two", "SUM", "z.average_cost_for_two")]))
    intent = SemanticIntent(metric="average_payment_amount")
    new, notes = _unasked_average(intent, "Revenue by month", registry)
    assert new.metric == "total_payment_amount" and notes
    # Asked for, no sum word, or a column that is already an average (the 11.45M bug): kept.
    assert _unasked_average(intent, "Average revenue by month", registry)[0] == intent
    assert _unasked_average(intent, "Payment amount by month", registry)[0] == intent
    cost = SemanticIntent(metric="average_cost_for_two")
    assert _unasked_average(cost, "Total cost for two in Delhi", registry)[0] == cost


def test_a_terse_follow_up_keeps_the_previous_breakdown_and_dates():
    import datetime

    from app.agent.nodes.intent_mapper import _terse_follow_up

    registry = _invoice_registry()
    previous = {"metric": "total_invoice_amount", "dimensions": ["billing_country"],
                "start_date": "2018-01-01", "end_date": "2018-12-31"}
    lost = SemanticIntent(metric="total_invoice_amount", limit=3)
    new, notes = _terse_follow_up(lost, "Top 3 only", registry, previous)
    assert new.dimensions == ["billing_country"] and new.start_date == datetime.date(2018, 1, 1) and len(notes) == 2
    year, _ = _terse_follow_up(lost, "Just 2017", registry, previous)
    assert (year.start_date, year.end_date) == (datetime.date(2017, 1, 1), datetime.date(2017, 12, 31))
    # "In total" drops the breakdown on purpose; a long question, or no previous turn: left alone.
    assert _terse_follow_up(lost, "In total", registry, previous)[0].dimensions == []
    assert _terse_follow_up(lost, "What is the total invoice amount for every year", registry, previous)[0] == lost
    assert _terse_follow_up(lost, "Top 3 only", registry, None)[0] == lost


def test_a_narrowing_follow_up_keeps_the_previous_question_and_adds_its_filter():
    """ "Only for customers in Canada" after "Revenue by month" came back as a customer count with no
    months (Pagila, run 12, twice)."""
    from app.agent.nodes.intent_mapper import _narrowing_follow_up
    from app.connectors.base import SchemaContext

    schema = SchemaContext(tables={t.name: [] for t in REGISTRY.schema.tables}, row_counts={}, join_paths=[])
    previous = {"metric": "total_revenue", "time_dimension": "order_date", "time_granularity": "month",
                "order_by": "time_asc"}
    swapped = SemanticIntent(metric="order_count",
                             filters=[FilterCondition(dimension="customer_state", operator="eq", value="SP")])
    new, notes = _narrowing_follow_up(swapped, "Only for customers in SP", REGISTRY, schema, previous)
    assert (new.metric, new.time_dimension, new.time_granularity, new.order_by) == (
        "total_revenue", "order_date", "month", "time_asc")
    assert [f.value for f in new.filters] == ["SP"] and notes
    assert _narrowing_follow_up(swapped, "Just orders from SP", REGISTRY, schema, previous)[0].metric == "total_revenue"
    # A measure named, no narrowing word, or no previous turn: the model's reading stands.
    assert _narrowing_follow_up(swapped, "Only count the orders in SP", REGISTRY, schema, previous)[0] == swapped
    assert _narrowing_follow_up(swapped, "Orders from customers in SP", REGISTRY, schema, previous)[0] == swapped
    assert _narrowing_follow_up(swapped, "Only for customers in SP", REGISTRY, schema, None)[0] == swapped


def test_a_what_about_follow_up_changes_only_the_measure():
    """ "What about the average length?" after "Number of films by rating" came back as one overall
    average (Pagila, runs 11-12)."""
    from app.agent.nodes.intent_mapper import _measure_swap_follow_up
    from app.connectors.base import SchemaContext

    schema = SchemaContext(tables={t.name: [] for t in REGISTRY.schema.tables}, row_counts={}, join_paths=[])
    previous = {"metric": "order_count", "dimensions": ["customer_state"], "start_date": "2018-01-01",
                "end_date": "2018-12-31"}
    flat = SemanticIntent(metric="average_order_value")
    new, notes = _measure_swap_follow_up(flat, "What about the average order value?", REGISTRY, schema, previous)
    assert (new.metric, new.dimensions, str(new.start_date)) == ("average_order_value", ["customer_state"], "2018-01-01")
    assert notes
    assert _measure_swap_follow_up(flat, "Average order value instead", REGISTRY, schema, previous)[0].dimensions == [
        "customer_state"]
    # "Overall", no cue, a breakdown already chosen, or no previous turn: the model's reading stands.
    assert _measure_swap_follow_up(flat, "What about the overall average order value?", REGISTRY, schema, previous)[0] == flat
    assert _measure_swap_follow_up(flat, "Average order value", REGISTRY, schema, previous)[0] == flat
    by_status = SemanticIntent(metric="average_order_value", dimensions=["order_status"])
    assert _measure_swap_follow_up(by_status, "What about by status?", REGISTRY, schema, previous)[0] == by_status
    assert _measure_swap_follow_up(flat, "What about the average order value?", REGISTRY, schema, None)[0] == flat


def test_number_of_things_replaces_a_count_of_something_else():
    from app.agent.nodes.intent_mapper import _how_many_counts
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticMetric

    registry = _invoice_registry()
    lines = SemanticMetric(name="total_invoice_lines", display_name="Total Invoice Lines", description="n",
                           aggregation="COUNT_DISTINCT", sql_expression="invoice_line.x", required_tables=["invoice_line"])
    both = SemanticRegistry(registry.schema.model_copy(update={"metrics": [*registry.schema.metrics, lines]}))
    wrong = SemanticIntent(metric="total_invoice_lines", dimensions=["billing_country"])
    assert _how_many_counts(wrong, "Number of invoices by country", both)[0].metric == "invoices"
    # The count the question names, asked for by its own words: kept.
    assert _how_many_counts(wrong, "Number of invoice lines by country", both)[0] == wrong
    right = SemanticIntent(metric="invoices")
    assert _how_many_counts(right, "Number of invoices by country", both)[0] == right


# Review findings on the rule chain.
def test_a_filter_the_question_names_is_kept_with_its_breakdown():
    both = SemanticIntent(metric="total_revenue", dimensions=["product_category"], filters=[
        FilterCondition(dimension="product_category", operator="eq", value="toys")])
    kept, notes = _drop_redundant_breakdown(both, "Revenue for toys, broken down by product category", REGISTRY, set())
    assert kept == both and notes == []


class _TwoFlags:
    def values(self, dimension):
        return {"Yes", "No"} if dimension in ("order_status", "payment_type") else set()


def test_each_flag_takes_the_polarity_of_its_own_mention():
    from app.agent.nodes.intent_mapper import _flag_filters

    grouped = SemanticIntent(metric="order_count", dimensions=["order_status", "payment_type"])
    new, _ = _flag_filters(grouped, "Orders without order status that have a payment type", REGISTRY, _TwoFlags())
    assert {(f.dimension, f.value) for f in new.filters} == {("order_status", "No"), ("payment_type", "Yes")}


def test_a_short_new_question_does_not_inherit_the_previous_dates():
    from datetime import date

    from app.agent.nodes.intent_mapper import _terse_follow_up

    previous = {"metric": "total_revenue", "dimensions": ["customer_state"],
                "start_date": "2018-01-01", "end_date": "2018-12-31"}
    new_question = SemanticIntent(metric="order_count")
    assert _terse_follow_up(new_question, "How many orders?", REGISTRY, previous) == (new_question, [])
    same_metric = SemanticIntent(metric="total_revenue")
    kept, _ = _terse_follow_up(same_metric, "Top 3 only", REGISTRY, previous)
    assert kept.start_date == date(2018, 1, 1) and kept.dimensions == ["customer_state"]


def test_instead_inside_a_new_question_is_not_a_measure_swap():
    from app.agent.nodes.intent_mapper import _MEASURE_SWAP

    assert _MEASURE_SWAP.search("what about the average price")
    assert _MEASURE_SWAP.search("the average price instead?")
    assert not _MEASURE_SWAP.search("count orders instead of payments")


def test_a_value_that_is_part_of_its_columns_name_is_not_taken_as_named():
    # "Average discount by ship mode" became l_shipmode = 'SHIP' (TPC-H, benchmark): "ship" names the
    # column there. The registry's order_status stands in for l_shipmode.
    from app.agent.nodes.intent_mapper import _part_of_its_name

    status = REGISTRY.get_dimension("order_status")
    assert _part_of_its_name("ORDER", status) and _part_of_its_name("Status", status)
    assert not _part_of_its_name("delivered", status) and not _part_of_its_name("or", status)
    grouped = SemanticIntent(metric="order_count", dimensions=["order_status"])
    assert _filter_named_values(grouped, "Which order status has the most orders?", REGISTRY,
                                [_hint("order_status", "ORDER")])[0] == grouped
