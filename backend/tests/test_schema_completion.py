from __future__ import annotations

import pytest

from app.connectors import DuckDBConnector
from app.models import SemanticIntent
from app.semantic import SemanticRegistry, build_sql_from_intent
from app.services.schema_completion import SchemaProposal, complete_schema
from app.services.schema_generator import _build_physical_summary


@pytest.fixture(scope="module")
def ctx():
    return DuckDBConnector().get_schema()


def _proposed(schema):
    """The metrics that came from the proposal (not the coverage metrics added after it)."""
    return [m for m in schema.metrics if "(added for coverage)" not in m.description]


def _proposal(metrics=(), dimensions=(), time_dimensions=()):
    return SchemaProposal.model_validate({"dataset": "Olist Orders", "metrics": list(metrics),
                                          "dimensions": list(dimensions), "time_dimensions": list(time_dimensions)})


def test_undeclared_alias_wrong_table_and_bare_columns_are_qualified(ctx):
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "Total Revenue", "aggregation": "SUM", "sql_expression": "op.payment_value"},          # alias
        {"name": "avg_price", "aggregation": "AVG", "sql_expression": "orders.price"},                 # wrong table
        {"name": "freight", "aggregation": "SUM", "sql_expression": "CAST(freight_value AS DOUBLE)"},  # bare column
    ]), ctx)
    by_name = {m.name: m for m in _proposed(schema)}
    assert by_name["total_revenue"].sql_expression == "order_payments.payment_value"
    assert by_name["total_revenue"].required_tables == ["order_payments"]
    assert by_name["avg_price"].sql_expression == "order_items.price"
    assert by_name["freight"].sql_expression == "CAST(order_items.freight_value AS DOUBLE)"
    assert any("repaired metric total_revenue" in n for n in notes)


def test_ambiguous_or_unknown_columns_are_dropped_not_guessed(ctx):
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "orders_n", "aggregation": "COUNT_DISTINCT", "sql_expression": "order_id"},       # in 5 tables
        {"name": "ghost", "aggregation": "SUM", "sql_expression": "orders.not_a_column"},
        {"name": "ok", "aggregation": "COUNT_DISTINCT", "sql_expression": "orders.order_id"},
    ]), ctx)
    assert [m.name for m in _proposed(schema)] == ["ok"]
    assert sum("dropped metric" in n for n in notes) == 2


def test_tables_follow_first_use_and_literals_are_left_alone(ctx):
    schema, _ = complete_schema(_proposal(metrics=[
        {"name": "cancel_rate", "aggregation": "RATIO",
         "numerator_sql": "CASE WHEN o.order_status = 'canceled.x' THEN 1 ELSE 0 END", "denominator_sql": "1"},
        {"name": "late_revenue", "aggregation": "SUM", "sql_expression": "order_payments.payment_value",
         "base_filter": "orders.order_status = 'delivered'"},
    ]), ctx)
    ratio, late = _proposed(schema)
    assert ratio.numerator_sql == "CASE WHEN orders.order_status = 'canceled.x' THEN 1 ELSE 0 END"
    assert ratio.required_tables == ["orders"]
    assert late.required_tables == ["order_payments", "orders"]  # the measure's table is the base


def test_completed_schema_compiles_and_runs_on_the_demo(ctx):
    schema, _ = complete_schema(_proposal(
        metrics=[{"name": "revenue", "aggregation": "SUM", "sql_expression": "payment_value"}],
        dimensions=[{"name": "state", "sql_expression": "customers.customer_state", "cardinality": "low"}],
        time_dimensions=[{"name": "order_date", "sql_expression": "order_purchase_timestamp"}],
    ), ctx)
    assert schema.time_dimensions[0].table == "orders"
    assert {j.on for j in schema.joins} == {j["on"] for j in ctx.join_paths + ctx.inferred_joins}
    registry = SemanticRegistry(schema)
    connector = DuckDBConnector()
    sql = build_sql_from_intent(SemanticIntent(metric="revenue", dimensions=["state"], time_dimension="order_date",
                                               time_granularity="month", limit=5), registry)
    assert len(connector.execute_query(sql, limit=5)) == 5


def test_time_dimension_must_live_in_one_table(ctx):
    schema, notes = complete_schema(_proposal(
        metrics=[{"name": "n", "aggregation": "COUNT_DISTINCT", "sql_expression": "orders.order_id"}],
        time_dimensions=[{"name": "odd", "sql_expression": "COALESCE(orders.order_purchase_timestamp, "
                                                          "order_items.shipping_limit_date)"}],
    ), ctx)
    # Dropped; the date columns are then added one table each by coverage.
    assert "odd" not in {t.name for t in schema.time_dimensions} and any("spans tables" in n for n in notes)
    assert all("," not in t.sql_expression for t in schema.time_dimensions)


def test_physical_summary_is_compact_and_skips_samples_for_ids(ctx):
    summary = _build_physical_summary(ctx)
    assert "TABLE orders (" in summary and "JOINS" in summary
    assert "customer_state VARCHAR, 27 distinct, e.g." in summary or "customer_state VARCHAR, 28 distinct, e.g." in summary
    id_line = next(line for line in summary.splitlines() if line.strip().startswith("customer_id "))
    assert "e.g." not in id_line
    assert len(summary) < 6000  # was ~16k characters of indented JSON


def test_validator_probes_time_dimensions_and_counts_nothing_unprobed(ctx):
    from app.services.schema_validator import validate_schema

    schema, _ = complete_schema(_proposal(
        metrics=[{"name": "revenue", "aggregation": "SUM", "sql_expression": "order_payments.payment_value"},
                 {"name": "broken", "aggregation": "AVG", "sql_expression": "CAST(order_items.price AS NOT_A_TYPE)"}],
        dimensions=[{"name": "state", "sql_expression": "customers.customer_state"}],
        time_dimensions=[{"name": "order_date", "sql_expression": "orders.order_purchase_timestamp"},
                         {"name": "bad_date", "sql_expression": "DATE_TRUNC('fortnight', orders.order_approved_at)"}],
    ), ctx)
    summary = validate_schema(schema, DuckDBConnector(), ctx)
    assert summary.broken_metrics == ["broken"] and summary.valid_metrics == len(schema.metrics) - 1
    assert "state" not in summary.broken_dimensions
    assert summary.valid_dimensions == len(schema.dimensions) - len(summary.broken_dimensions)
    # The proposal's bad date is caught; the dates completion adds for metric tables are probed too.
    assert summary.broken_time_dimensions == ["bad_date"]
    assert summary.valid_time_dimensions == summary.total_time_dimensions - 1

    only_broken = schema.model_copy(update={"metrics": [m for m in schema.metrics if m.name == "broken"]})
    summary = validate_schema(only_broken, DuckDBConnector(), ctx)
    assert summary.valid_dimensions == 0 and "state" in summary.broken_dimensions
    assert summary.valid_time_dimensions == 0


def test_measure_columns_the_llm_left_out_get_metrics_up_to_the_cap(ctx):
    from app.services.schema_completion import MAX_METRICS

    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "revenue", "aggregation": "SUM", "sql_expression": "order_payments.payment_value"}]), ctx)
    names = [m.name for m in schema.metrics]
    assert names[0] == "revenue" and len(names) == MAX_METRICS
    assert "total_payment_value" not in names  # already covered by the proposal's SUM
    # Measure names first; a generic column name carries its table in a multi-table database.
    assert {"total_order_items_price", "average_order_items_price", "total_freight_value"} <= set(names)
    assert {"orders", "customers", "products", "sellers"} <= set(names)  # a count per entity table
    # order_item_id numbers items within an order (not a key): the count is of the table's rows
    assert schema.metrics[names.index("items")].aggregation == "COUNT"
    assert not any(n.endswith(("_id", "zip_code_prefix", "sequential")) for n in names)
    assert all(m.required_tables == [m.sql_expression.split(".")[0]] for m in schema.metrics[1:])
    assert sum(n.startswith("added metric") for n in notes) == MAX_METRICS - 1


def _zomato_ctx():
    from app.connectors.base import SchemaContext

    columns = [("restaurant_id", "BIGINT", 9551), ("restaurant_name", "VARCHAR", 7446), ("country_code", "BIGINT", 15),
               ("city", "VARCHAR", 141), ("address", "VARCHAR", 8918), ("locality_verbose", "VARCHAR", 1265),
               ("longitude", "DOUBLE", 8120), ("latitude", "DOUBLE", 8677), ("average_cost_for_two", "BIGINT", 140),
               ("currency", "VARCHAR", 12), ("has_table_booking", "VARCHAR", 2), ("switch_to_order_menu", "VARCHAR", 1),
               ("price_range", "BIGINT", 4), ("aggregate_rating", "DOUBLE", 33), ("rating_text", "VARCHAR", 6),
               ("votes", "BIGINT", 1012)]
    return SchemaContext(tables={"zomato": [{"name": n, "type": t} for n, t, _ in columns]},
                         row_counts={"zomato": 9551}, join_paths=[],
                         distinct_counts={"zomato": {n: d for n, _, d in columns}})


def test_left_out_categorical_columns_become_dimensions():
    schema, _ = complete_schema(_proposal(
        metrics=[{"name": "restaurants", "aggregation": "COUNT_DISTINCT", "sql_expression": "zomato.restaurant_id"}],
        dimensions=[{"name": "city", "sql_expression": "zomato.city"}]), _zomato_ctx())
    dims = {d.name: d for d in schema.dimensions}
    for expected in ("price_range", "has_table_booking", "rating_text", "currency", "country_code", "restaurant_name"):
        assert expected in dims, expected
    assert dims["restaurant_name"].cardinality == "high" and dims["price_range"].cardinality == "low"
    # Free text, coordinates, single-valued columns, IDs and wide numbers are not breakdowns.
    for absent in ("address", "locality_verbose", "latitude", "longitude", "switch_to_order_menu", "restaurant_id",
                   "votes", "average_cost_for_two"):
        assert absent not in dims, absent


def test_scores_levels_and_coordinates_are_never_summed():
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "total_rating", "aggregation": "SUM", "sql_expression": "zomato.aggregate_rating"},
        {"name": "restaurants", "aggregation": "COUNT_DISTINCT", "sql_expression": "zomato.restaurant_id"}]),
        _zomato_ctx())
    names = {m.name for m in schema.metrics}
    assert "total_rating" not in names and any("dropped metric total_rating" in n for n in notes)
    assert {"average_aggregate_rating", "average_price_range", "total_votes", "total_average_cost_for_two"} <= names
    assert not names & {"total_aggregate_rating", "total_price_range", "total_latitude", "average_latitude",
                        "total_longitude", "average_longitude"}


class _ProfileConnector:
    """Answers the zero-profile query with fixed figures per column."""

    def __init__(self, figures):
        self.figures = figures

    def table_ref_resolver(self):
        return None

    def execute_query(self, sql, limit=5000):
        import pandas as pd

        column = next(c for c in self.figures if f"zomato.{c} = 0" in sql)
        n, zeros, low, high = self.figures[column]
        return pd.DataFrame([{"n": n, "zeros": zeros, "low": low, "high": high}])


def test_averages_skip_zeros_that_mean_not_recorded():
    from app.services.schema_generator import exclude_placeholder_zeros

    schema, _ = complete_schema(_proposal(metrics=[
        {"name": "restaurants", "aggregation": "COUNT_DISTINCT", "sql_expression": "zomato.restaurant_id"}]),
        _zomato_ctx())
    connector = _ProfileConnector({
        "aggregate_rating": (9551, 2148, 1.8, 4.9),        # 22% zeros, real ratings 1.8-4.9: placeholder
        "votes": (9551, 1094, 1, 10934),                   # zeros, but real values start at 1: genuine zeros
        "price_range": (9551, 0, 1, 4),                    # no zeros
        "average_cost_for_two": (9551, 18, 7, 800000),     # rare zeros
    })
    by_name = {m.name: m for m in exclude_placeholder_zeros(schema, connector).metrics}
    # The original average is never redefined; a variant without the zeros is proposed next to it.
    assert by_name["average_aggregate_rating"].sql_expression == "zomato.aggregate_rating"
    variant = by_name["average_aggregate_rating_excluding_zero"]
    assert variant.sql_expression == "NULLIF(zomato.aggregate_rating, 0)"
    assert variant.excludes_zeros_of == "average_aggregate_rating" and variant.zero_share == 0.225
    for name in ("average_votes", "average_price_range", "average_cost_for_two"):
        assert by_name[name].sql_expression.startswith("zomato."), name
        assert f"{name}_excluding_zero" not in by_name


def test_a_sum_is_never_called_an_average():
    """The column is named average_cost_for_two; SUM over it under that name answered 11,453,662."""
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "average_cost_for_two", "display_name": "Average Cost for Two", "aggregation": "SUM",
         "sql_expression": "zomato.average_cost_for_two"}]), _zomato_ctx())
    by_name = {m.name: m for m in schema.metrics}
    assert by_name["total_average_cost_for_two"].aggregation == "SUM"
    assert by_name["total_average_cost_for_two"].display_name == "Sum of Average Cost for Two"
    assert by_name["average_cost_for_two"].aggregation == "AVG"           # the real average keeps the name
    assert by_name["average_cost_for_two"].display_name == "Average Cost For Two"
    assert any("renamed metric average_cost_for_two" in n for n in notes)


def test_a_count_is_never_called_a_total():
    """Pagila: COUNT(payment_id) named total_payments beside SUM(amount); "which customers paid the
    most" took the count. Counts already named as counts keep their name."""
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "total_restaurants", "display_name": "Total Restaurants", "aggregation": "COUNT",
         "sql_expression": "zomato.restaurant_id"},
        {"name": "total_votes", "display_name": "Total Votes", "aggregation": "SUM", "sql_expression": "zomato.votes"},
        {"name": "total_rating_count", "display_name": "Total Rating Count", "aggregation": "COUNT",
         "sql_expression": "zomato.aggregate_rating"}]), _zomato_ctx())
    by_name = {m.name: m for m in schema.metrics}
    assert by_name["restaurants_count"].display_name == "Number of Restaurants"
    assert "total_restaurants" not in by_name
    assert by_name["total_votes"].aggregation == "SUM"                    # a real total keeps its name
    assert "total_rating_count" in by_name                                # already says it counts
    assert any("a count is not a total" in n for n in notes)


class _DistinctConnector:
    def __init__(self, values):
        self.values = values

    def table_ref_resolver(self):
        return None

    def execute_query(self, sql, limit=5000):
        import pandas as pd

        column = next(c for c in self.values if f".{c} AS v" in sql)
        return pd.DataFrame({"v": self.values[column]})


def test_money_metrics_are_linked_to_a_currency_column():
    from app.services.schema_generator import detect_unit_dimensions

    schema, _ = complete_schema(_proposal(
        metrics=[{"name": "restaurants", "aggregation": "COUNT_DISTINCT", "sql_expression": "zomato.restaurant_id"}]),
        _zomato_ctx())
    connector = _DistinctConnector({
        "currency": ["Indian Rupees(Rs.)", "Dollar($)", "Botswana Pula(P)", "Pounds(\ufffd\ufffd)", "Rand(R)"],
        "city": ["New Delhi", "Doha", "London"], "rating_text": ["Good", "Average"], "has_table_booking": ["Yes", "No"],
        "country_code": [1, 14], "price_range": [1, 2, 3, 4], "restaurant_name": ["Toit"],
    })
    by_name = {m.name: m for m in detect_unit_dimensions(schema, connector).metrics}
    assert by_name["average_cost_for_two"].unit_dimension == "currency"
    assert by_name["total_average_cost_for_two"].unit_dimension == "currency"
    for name in ("restaurants", "total_votes", "average_aggregate_rating", "average_price_range"):
        assert by_name[name].unit_dimension is None, name


def test_metrics_do_not_take_column_names():
    schema, _ = complete_schema(_proposal(
        metrics=[{"name": "city", "display_name": "Unique Cities", "aggregation": "COUNT_DISTINCT",
                  "sql_expression": "zomato.city"},
                 {"name": "restaurant_id", "aggregation": "COUNT_DISTINCT", "sql_expression": "zomato.restaurant_id"}],
        dimensions=[{"name": "city", "sql_expression": "zomato.city"}]), _zomato_ctx())
    assert {"unique_city", "unique_restaurant_id"} <= {m.name for m in schema.metrics}
    assert "city" in {d.name for d in schema.dimensions}


def test_date_columns_become_time_dimensions_when_the_llm_proposes_none(ctx):
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "revenue", "aggregation": "SUM", "sql_expression": "order_payments.payment_value"}]), ctx)
    from app.services.schema_completion import MAX_TIME_DIMENSIONS

    assert 1 <= len(schema.time_dimensions) <= MAX_TIME_DIMENSIONS and any("added time dimension" in n for n in notes)
    kept, _ = complete_schema(_proposal(
        metrics=[{"name": "revenue", "aggregation": "SUM", "sql_expression": "order_payments.payment_value"}],
        time_dimensions=[{"name": "order_date", "sql_expression": "orders.order_purchase_timestamp"}]), ctx)
    # The proposal's date stays first; other tables with metrics get one date each (Pagila: rentals
    # by month had only payment_date to group by).
    assert kept.time_dimensions[0].name == "order_date"
    tables = [t.table for t in kept.time_dimensions]
    assert len(tables) == len(set(tables)) and len(tables) <= MAX_TIME_DIMENSIONS


def test_entities_are_counted_by_their_id_not_their_name():
    # Chains share a name: COUNT(DISTINCT restaurant_name) undercounts restaurants.
    schema, notes = complete_schema(_proposal(
        metrics=[{"name": "unique_restaurant_name", "aggregation": "COUNT_DISTINCT",
                  "sql_expression": "zomato.restaurant_name"},
                 {"name": "cities", "aggregation": "COUNT_DISTINCT", "sql_expression": "zomato.city"}],
        dimensions=[{"name": "city", "sql_expression": "zomato.city"}]), _zomato_ctx())
    metrics = {m.name: m for m in schema.metrics}
    assert metrics["restaurants"].sql_expression == "zomato.restaurant_id"
    assert metrics["restaurants"].display_name == "Restaurants"
    assert "unique_restaurant_name" not in metrics
    assert metrics["cities"].sql_expression == "zomato.city"  # no city_id: left alone
    assert any("names repeat" in n for n in notes)


def _chinook_ctx():
    from app.connectors.base import SchemaContext

    tables = {
        "customer": [("customer_id", "INTEGER", 59), ("first_name", "VARCHAR", 57), ("last_name", "VARCHAR", 59),
                     ("email", "VARCHAR", 59), ("fax", "VARCHAR", 12), ("country", "VARCHAR", 24),
                     ("support_rep_id", "INTEGER", 3)],
        "employee": [("employee_id", "INTEGER", 8), ("first_name", "VARCHAR", 8), ("last_name", "VARCHAR", 8),
                     ("title", "VARCHAR", 5), ("reports_to", "INTEGER", 3), ("address", "VARCHAR", 8)],
        "genre": [("genre_id", "INTEGER", 25), ("name", "VARCHAR", 25)],
        "track": [("track_id", "INTEGER", 3503), ("name", "VARCHAR", 3257), ("genre_id", "INTEGER", 25),
                  ("milliseconds", "INTEGER", 3080), ("unit_price", "DECIMAL(10,2)", 2)],
        "playlist_track": [("playlist_id", "INTEGER", 14), ("track_id", "INTEGER", 3503)],
    }
    rows = {"customer": 59, "employee": 8, "genre": 25, "track": 3503, "playlist_track": 8715}
    joins = [{"from": "customer", "to": "employee", "type": "LEFT", "on": "customer.support_rep_id = employee.employee_id"},
             {"from": "employee", "to": "employee", "type": "LEFT", "on": "employee.reports_to = employee.employee_id"},
             {"from": "track", "to": "genre", "type": "LEFT", "on": "track.genre_id = genre.genre_id"}]
    return SchemaContext(tables={t: [{"name": n, "type": ty} for n, ty, _ in cols] for t, cols in tables.items()},
                         row_counts=rows, join_paths=joins[:1] + joins[2:],
                         distinct_counts={t: {n: d for n, _, d in cols} for t, cols in tables.items()},
                         inferred_joins=[])


def test_multi_table_coverage_counts_entities_names_people_and_skips_contact_details():
    ctx = _chinook_ctx()
    ctx.join_paths.append({"from": "employee", "to": "employee", "type": "LEFT",
                           "on": "employee.reports_to = employee.employee_id"})
    schema, _ = complete_schema(_proposal(metrics=[
        {"name": "revenue", "aggregation": "SUM", "sql_expression": "track.unit_price"}]), ctx)
    metrics = {m.name: m for m in schema.metrics}
    for entity in ("customers", "employees", "genres", "tracks"):
        assert metrics[entity].aggregation == "COUNT_DISTINCT", entity
    assert "playlist_tracks" not in metrics  # a link table: only other tables' IDs
    assert not any("reports_to" in (m.sql_expression or "") for m in schema.metrics)  # a join key
    dims = {d.name: d for d in schema.dimensions}
    assert dims["employee_name"].sql_expression == "employee.first_name || ' ' || employee.last_name"
    assert dims["employee_name"].display_name == "Employee Name" and "customer_name" in dims
    assert {"genre_name", "employee_title", "country"} <= set(dims)
    assert not {"email", "fax", "address", "first_name", "last_name", "name"} & set(dims)


def test_shared_column_names_carry_their_table_and_lines_get_an_amount():
    ctx = _chinook_ctx()
    ctx.tables["customer"].append({"name": "city", "type": "VARCHAR"})
    ctx.tables["employee"].append({"name": "city", "type": "VARCHAR"})
    ctx.distinct_counts["customer"]["city"], ctx.distinct_counts["employee"]["city"] = 53, 3
    ctx.tables["invoice_line"] = [{"name": n, "type": t} for n, t in (
        ("invoice_line_id", "INTEGER"), ("track_id", "INTEGER"), ("unit_price", "DECIMAL(10,2)"), ("quantity", "INTEGER"))]
    ctx.distinct_counts["invoice_line"] = {"invoice_line_id": 2240, "track_id": 1984, "unit_price": 2, "quantity": 1}
    ctx.row_counts["invoice_line"] = 2240
    ctx.key_columns = {("employee", "reports_to"), ("employee", "employee_id")}  # a self-reference: kept as a key
    schema, _ = complete_schema(_proposal(), ctx)
    dims = {d.name: d for d in schema.dimensions}
    assert dims["customer_city"].sql_expression == "customer.city" and dims["employee_city"].sql_expression == "employee.city"
    assert "city" not in dims and "reports_to" not in dims
    metrics = {m.name: m for m in schema.metrics}
    assert metrics["total_invoice_line_amount"].sql_expression == "invoice_line.unit_price * invoice_line.quantity"
    assert {"average_track_unit_price", "average_invoice_line_unit_price"} <= set(metrics)  # not one "unit_price"
    assert not any("reports_to" in (m.sql_expression or "") for m in schema.metrics)


def test_a_price_column_is_not_a_unit_column():
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable
    from app.services.schema_generator import detect_unit_dimensions

    schema = SemanticSchema(
        version="1.0", dataset="store", description="d", tables=[SemanticTable(name="invoice_line")], joins=[],
        metrics=[SemanticMetric(name="total_invoice_line_amount", display_name="Amount", description="Amount",
                                aggregation="SUM", sql_expression="invoice_line.unit_price * invoice_line.quantity",
                                required_tables=["invoice_line"])],
        dimensions=[SemanticDimension(name="unit_price", display_name="Unit Price", required_tables=["invoice_line"],
                                      sql_expression="invoice_line.unit_price", cardinality="low")],
        time_dimensions=[])
    connector = _DistinctConnector({"unit_price": [0.99, 1.99]})
    assert detect_unit_dimensions(schema, connector).metrics[0].unit_dimension is None


def test_a_foreign_key_named_after_a_role_names_the_row_it_points_at():
    schema, _ = complete_schema(_proposal(), _chinook_ctx())  # customer.support_rep_id -> employee
    dims = {d.name: d for d in schema.dimensions}
    assert dims["support_rep_name"].display_name == "Support Rep Name"
    assert dims["support_rep_name"].sql_expression == dims["employee_name"].sql_expression
    assert "genre_name" in dims and not any(n.startswith(("genre_id", "customer_name_")) for n in dims)
    assert sum(d.sql_expression == dims["employee_name"].sql_expression for d in schema.dimensions) == 2


def test_proposed_key_dimensions_give_way_to_the_name_they_point_at():
    ctx = _chinook_ctx()  # playlists have no table here: their key stays (like Pagila's store_id)
    ctx.join_paths.append({"from": "playlist_track", "to": "playlist", "type": "LEFT",
                           "on": "playlist_track.playlist_id = playlist.playlist_id"})
    schema, notes = complete_schema(_proposal(dimensions=[
        {"name": "customer_support_rep_id", "sql_expression": "customer.support_rep_id"},
        {"name": "track_genre_id", "sql_expression": "track.genre_id"},
        {"name": "track_id", "sql_expression": "track.track_id"},
        {"name": "playlist_id", "sql_expression": "playlist_track.playlist_id"}]), ctx)
    dims = {d.name for d in schema.dimensions}
    assert not dims & {"customer_support_rep_id", "track_genre_id", "track_id"}
    assert {"playlist_id", "support_rep_name", "genre_name", "track_name"} <= dims
    assert "dropped dimension track_genre_id: track.genre_id is a key of genre, which has a name" in notes


def _rental_ctx():
    from app.connectors.base import SchemaContext

    tables = {
        "rental": [("rental_id", "INTEGER", 16044), ("rental_date", "TIMESTAMP", 15815), ("last_update", "TIMESTAMP", 3)],
        "payment": [("payment_id", "INTEGER", 16049), ("amount", "DECIMAL(5,2)", 19), ("payment_date", "TIMESTAMP", 16000)],
        "film": [("film_id", "INTEGER", 1000), ("rental_rate", "DECIMAL(4,2)", 3), ("last_update", "TIMESTAMP", 1)],
        "address": [("address_id", "INTEGER", 603), ("district", "VARCHAR", 378)],
        "language": [("language_id", "INTEGER", 6), ("name", "VARCHAR", 6)],
    }
    rows = {"rental": 16044, "payment": 16049, "film": 1000, "address": 603, "language": 6}
    return SchemaContext(tables={t: [{"name": n, "type": ty} for n, ty, _ in cols] for t, cols in tables.items()},
                         row_counts=rows, join_paths=[],
                         distinct_counts={t: {n: d for n, _, d in cols} for t, cols in tables.items()})


def test_entity_counts_favour_large_tables_and_singular_names_ending_in_s():
    import app.services.schema_completion as sc

    schema, _ = complete_schema(_proposal(metrics=[
        {"name": "revenue", "aggregation": "SUM", "sql_expression": "payment.amount"}]), _rental_ctx())
    names = [m.name for m in schema.metrics]
    assert "addresses" in names and "number_of_address" not in names  # "address" is singular
    original = sc.MAX_ENTITY_COUNTS
    try:
        sc.MAX_ENTITY_COUNTS = 2
        schema, _ = complete_schema(_proposal(), _rental_ctx())
        counts = [m.name for m in schema.metrics if "(added for coverage)" in m.description
                  and m.aggregation == "COUNT_DISTINCT"]
        assert counts == ["payments", "rentals"]  # the two largest tables, not languages
    finally:
        sc.MAX_ENTITY_COUNTS = original


def test_each_table_with_a_metric_gets_its_date_but_not_an_audit_stamp():
    schema, _ = complete_schema(_proposal(
        metrics=[{"name": "revenue", "aggregation": "SUM", "sql_expression": "payment.amount"},
                 {"name": "rentals", "aggregation": "COUNT_DISTINCT", "sql_expression": "rental.rental_id"},
                 {"name": "average_rate", "aggregation": "AVG", "sql_expression": "film.rental_rate"}],
        time_dimensions=[{"name": "payment_date", "sql_expression": "payment.payment_date"}]), _rental_ctx())
    by_table = {t.table: t.sql_expression for t in schema.time_dimensions}
    assert by_table["payment"] == "payment.payment_date" and by_table["rental"] == "rental.rental_date"
    assert "film" not in by_table  # film has only last_update


def test_per_unit_figures_are_never_summed_and_a_file_without_ids_counts_its_rows():
    from app.connectors.base import SchemaContext

    columns = [("symbol", "VARCHAR", 505), ("name", "VARCHAR", 505), ("sector", "VARCHAR", 11),
               ("price", "DOUBLE", 480), ("price_earnings", "DOUBLE", 400), ("dividend_yield", "DOUBLE", 420),
               ("earnings_share", "DOUBLE", 450), ("c_52_week_high", "DOUBLE", 490), ("market_cap", "BIGINT", 505),
               ("ebitda", "BIGINT", 480)]
    ctx = SchemaContext(tables={"sp500": [{"name": n, "type": t} for n, t, _ in columns]}, row_counts={"sp500": 505},
                        join_paths=[], distinct_counts={"sp500": {n: d for n, _, d in columns}}, inferred_joins=[])
    schema, _ = complete_schema(_proposal(), ctx)
    metrics = {m.name: m for m in schema.metrics}
    for ratio in ("price_earnings", "dividend_yield", "earnings_share", "c_52_week_high"):
        assert f"average_{ratio}" in metrics and f"total_{ratio}" not in metrics, ratio
    assert {"total_market_cap", "total_ebitda"} <= set(metrics)
    assert metrics["records"].sql_expression == "sp500.name" and metrics["records"].aggregation == "COUNT"


def test_a_unit_price_beside_a_quantity_is_averaged_not_summed():
    from app.connectors.base import SchemaContext

    columns = [("order_id", "INTEGER", 400), ("unit_price", "DECIMAL(19,4)", 60), ("quantity", "DECIMAL(18,4)", 40)]
    ctx = SchemaContext(tables={"lines": [{"name": n, "type": t} for n, t, _ in columns]}, row_counts={"lines": 900},
                        join_paths=[], distinct_counts={"lines": {n: d for n, _, d in columns}}, inferred_joins=[])
    names = {m.name for m in complete_schema(_proposal(), ctx)[0].metrics}
    assert "average_unit_price" in names and "total_unit_price" not in names and "total_lines_amount" in names


def test_northwind_shapes_constant_measures_flows_per_row_text_and_room_for_names():
    from app.connectors.base import SchemaContext

    tables = {
        "invoices": [("id", "INTEGER", 35), ("order_id", "INTEGER", 35), ("amount_due", "DECIMAL(19,4)", 1)],
        "orders": [("id", "INTEGER", 48), ("employee_id", "INTEGER", 9), ("shipping_fee", "DECIMAL(19,4)", 13)],
        "employees": [("id", "INTEGER", 9), ("first_name", "VARCHAR", 9), ("last_name", "VARCHAR", 9),
                      ("notes", "VARCHAR", 9), ("web_page", "VARCHAR", 9), ("job_title", "VARCHAR", 4)],
        "products": [("id", "INTEGER", 45), ("category", "VARCHAR", 16), ("list_price", "DECIMAL(19,4)", 30),
                     ("minimum_reorder_quantity", "INTEGER", 8)],
    }
    # Small lookup tables, as in Northwind: each with a label that competes for a slot.
    tables.update({f"lookup_{i}": [("id", "INTEGER", 3), (f"label_{i}", "VARCHAR", 3)] for i in range(22)})
    joins = [{"from": "invoices", "to": "orders", "type": "LEFT", "on": "invoices.order_id = orders.id",
              "relationship": "many_to_one"},
             {"from": "orders", "to": "employees", "type": "LEFT", "on": "orders.employee_id = employees.id",
              "relationship": "many_to_one"}]
    ctx = SchemaContext(tables={t: [{"name": n, "type": ty} for n, ty, _ in cols] for t, cols in tables.items()},
                        row_counts={t: max(d for _, _, d in cols) for t, cols in tables.items()}, join_paths=joins,
                        distinct_counts={t: {n: d for n, _, d in cols} for t, cols in tables.items()}, inferred_joins=[])
    proposed = [{"name": f"llm_{i}", "sql_expression": f"lookup_{i}.label_{i}"} for i in range(16)]
    proposed += [{"name": "employees_notes", "sql_expression": "employees.notes"},
                 {"name": "employees_web_page", "sql_expression": "employees.web_page"}]
    schema, notes = complete_schema(_proposal(
        metrics=[{"name": "total_revenue", "aggregation": "SUM", "sql_expression": "invoices.amount_due"}],
        dimensions=proposed), ctx)
    metrics = {m.name for m in schema.metrics}
    dims = {d.name: d.sql_expression for d in schema.dimensions}
    assert "total_revenue" not in metrics and not any("amount_due" in m for m in metrics)
    assert "total_shipping_fee" in metrics or "total_orders_shipping_fee" in metrics
    assert "total_products_amount" not in metrics and "total_list_price" not in metrics
    assert not {"employees_notes", "employees_web_page"} & set(dims)
    assert "employees.first_name || ' ' || employees.last_name" in dims.values()
    assert "products.category" in dims.values() and len(dims) <= 30


def test_a_dimension_named_for_another_column_is_dropped():
    from app.services.schema_completion import _mislabeled

    # The two found in 110 stored schemas (Northwind, qwen3:4b), and look-alikes that are fine.
    assert _mislabeled("product_category", "products", "list_price")
    assert _mislabeled("payment_type", "invoices", "due_date")
    for name, table, column in [("film_category", "category", "name"), ("shipper", "shippers", "company"),
                                ("product_status", "products", "discontinued"), ("cuisine", "zomato", "cuisines"),
                                ("customer_order_name", "orders", "ship_name"), ("order_status", "orders_status", "status_name")]:
        assert not _mislabeled(name, table, column), name


def test_a_pack_size_is_no_unit_of_the_price():
    from app.connectors.base import SchemaContext
    from app.services.schema_generator import detect_unit_dimensions

    columns = [("id", "INTEGER", 45), ("list_price", "DECIMAL(19,4)", 30), ("quantity_per_unit", "VARCHAR", 30),
               ("category", "VARCHAR", 16)]
    ctx = SchemaContext(tables={"products": [{"name": n, "type": t} for n, t, _ in columns]},
                        row_counts={"products": 45}, join_paths=[], inferred_joins=[],
                        distinct_counts={"products": {n: d for n, _, d in columns}})
    schema, _ = complete_schema(_proposal(dimensions=[
        {"name": "quantity_per_unit", "sql_expression": "products.quantity_per_unit"}]), ctx)
    connector = _DistinctConnector({"quantity_per_unit": ["10 boxes x 20 bags", "24 - 12 oz bottles", "12 - 550 ml bottles"],
                                    "category": ["Beverages", "Condiments"]})
    assert all(m.unit_dimension is None for m in detect_unit_dimensions(schema, connector).metrics)


def _ctx(tables: dict, rows: dict):
    from app.connectors.base import SchemaContext

    return SchemaContext(tables={t: [{"name": n, "type": ty} for n, ty, _ in cols] for t, cols in tables.items()},
                         row_counts=rows, join_paths=[],
                         distinct_counts={t: {n: d for n, _, d in cols} for t, cols in tables.items()})


def test_keys_named_like_tpch_and_tables_without_a_key_still_get_counts():
    # The benchmark's TPC-H layer had no count of customers or line items (keys c_custkey,
    # l_orderkey + l_linenumber), and "how many line items" took a count of orders.
    ctx = _ctx({
        "customer": [("c_custkey", "INTEGER", 15000), ("c_nationkey", "INTEGER", 25), ("c_acctbal", "DECIMAL(15,2)", 14000)],
        "lineitem": [("l_orderkey", "INTEGER", 150000), ("l_linenumber", "INTEGER", 7), ("l_quantity", "DECIMAL(15,2)", 50)],
        "departments": [("dept_no", "VARCHAR", 9), ("dept_name", "VARCHAR", 9)],
    }, {"customer": 15000, "lineitem": 600000, "departments": 9})
    schema, _ = complete_schema(_proposal(), ctx)
    metrics = {m.name: m for m in schema.metrics}
    assert (metrics["customers"].aggregation, metrics["customers"].sql_expression) == ("COUNT_DISTINCT", "customer.c_custkey")
    assert (metrics["departments"].aggregation, metrics["departments"].sql_expression) == ("COUNT_DISTINCT", "departments.dept_no")
    assert (metrics["lineitems"].aggregation, metrics["lineitems"].sql_expression) == ("COUNT", "lineitem.l_orderkey")


def test_a_trips_file_with_an_attached_lookup_counts_its_rows_and_never_sums_codes():
    # The taxi upload: no trip count ("how many trips" took total fares), and "Total Locationid",
    # "Average Payment Type" among its metrics.
    ctx = _ctx({
        "trips": [("VendorID", "INTEGER", 3), ("PULocationID", "INTEGER", 260), ("payment_type", "BIGINT", 5),
                  ("fare_amount", "DOUBLE", 9000), ("passenger_count", "BIGINT", 10)],
        "taxi_zone_lookup": [("LocationID", "BIGINT", 265), ("Borough", "VARCHAR", 7), ("Zone", "VARCHAR", 260)],
    }, {"trips": 2964624, "taxi_zone_lookup": 265})
    schema, notes = complete_schema(_proposal(metrics=[
        {"name": "total_locationid", "aggregation": "SUM", "sql_expression": "taxi_zone_lookup.LocationID"}]), ctx)
    metrics = {m.name: m for m in schema.metrics}
    assert metrics["trips"].aggregation == "COUNT" and metrics["trips"].sql_expression.startswith("trips.")
    assert metrics["taxi_zone_lookups"].sql_expression == "taxi_zone_lookup.LocationID"
    assert "total_locationid" not in metrics and any("is a key or a code" in n for n in notes)
    assert not [m for m in metrics if "payment_type" in m or "location" in m or "vendor" in m]
    assert "total_fare_amount" in metrics


def test_camel_case_names_are_split():
    from app.services.schema_completion import _snake

    assert _snake("VendorID") == "vendor_id" and _snake("PULocationID") == "pu_location_id"
    assert _snake("tpep_pickup_datetime") == "tpep_pickup_datetime" and _snake("Price/Earnings") == "price_earnings"


def test_a_date_proposed_as_a_dimension_becomes_a_time_dimension():
    # The employees layer had hire_date and birth_date as dimensions: "hired each year" grouped by
    # single days, and "time_dimension: hire_date" was rejected as unknown (benchmark, 0d).
    ctx = _ctx({"employees": [("emp_no", "INTEGER", 300000), ("hire_date", "DATE", 5000), ("gender", "VARCHAR", 2)]},
               {"employees": 300000})
    schema, notes = complete_schema(_proposal(dimensions=[
        {"name": "hire_date", "sql_expression": "employees.hire_date"},
        {"name": "gender", "sql_expression": "employees.gender"}]), ctx)
    assert "hire_date" not in {d.name for d in schema.dimensions} and "gender" in {d.name for d in schema.dimensions}
    assert [(t.name, t.sql_expression) for t in schema.time_dimensions] == [("hire_date", "employees.hire_date")]


def test_a_count_of_some_column_does_not_stand_in_for_the_row_count():
    ctx = _ctx({"trips": [("vendorid", "INTEGER", 3), ("passenger_count", "BIGINT", 10), ("fare_amount", "DOUBLE", 9000)]},
               {"trips": 2964624})
    schema, _ = complete_schema(_proposal(metrics=[
        {"name": "total_passenger_count", "aggregation": "COUNT", "sql_expression": "trips.passenger_count"}]), ctx)
    names = {m.name: m for m in schema.metrics}
    assert names["records"].sql_expression == "trips.vendorid"


def test_a_row_count_is_named_after_what_a_row_is_and_small_codes_and_unique_names_are_breakdowns():
    from app.services.schema_completion import _row_noun

    assert _row_noun("yellow_tripdata_2024_01") == "trip" and _row_noun("order_items") == "items"
    ctx = _ctx({
        "yellow_tripdata_2024_01": [("vendorid", "INTEGER", 3), ("fare_amount", "DOUBLE", 9000)],
        "customer": [("c_custkey", "INTEGER", 15000), ("c_name", "VARCHAR", 15000)],
    }, {"yellow_tripdata_2024_01": 2964624, "customer": 15000})
    schema, _ = complete_schema(_proposal(), ctx)
    assert "trips" in {m.name for m in schema.metrics}
    expressions = {d.sql_expression for d in schema.dimensions}
    assert {"yellow_tripdata_2024_01.vendorid", "customer.c_name"} <= expressions
