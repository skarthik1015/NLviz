from pathlib import Path

import duckdb
import pytest

from app.models import SemanticIntent
from app.semantic import SemanticRegistry, build_sql_from_intent, load_semantic_registry
from app.semantic.models import (
    SemanticDimension,
    SemanticJoin,
    SemanticMetric,
    SemanticSchema,
    SemanticTable,
    SemanticTimeDimension,
)


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"


def _make_registry(*, joins, metrics, dimensions) -> SemanticRegistry:
    schema = SemanticSchema(
        version="1.0",
        dataset="test",
        description="test schema",
        tables=[
            SemanticTable(name="orders"),
            SemanticTable(name="order_payments"),
            SemanticTable(name="a"),
            SemanticTable(name="b"),
            SemanticTable(name="c"),
            SemanticTable(name="d"),
        ],
        joins=joins,
        metrics=metrics,
        dimensions=dimensions,
        time_dimensions=[
            SemanticTimeDimension(
                name="order_date",
                display_name="Order Date",
                sql_expression="orders.created_at",
                default_granularity="month",
                table="orders",
            )
        ],
    )
    return SemanticRegistry(schema)


def test_from_clause_no_duplicate_alias_when_same_name():
    registry = load_semantic_registry(SCHEMA_PATH)
    sql = build_sql_from_intent(SemanticIntent(metric="order_count", limit=10), registry)

    from_line = next(line for line in sql.splitlines() if line.startswith("FROM "))
    assert from_line == "FROM orders"
    assert "FROM orders orders" not in sql

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id INTEGER)")
    conn.execute(sql).fetchall()


def test_from_clause_with_alias_when_different():
    registry = _make_registry(
        joins=[],
        metrics=[
            SemanticMetric(
                name="aliased_order_count",
                display_name="Aliased Order Count",
                description="Count orders from aliased base table",
                aggregation="COUNT_DISTINCT",
                sql_expression="o.order_id",
                required_tables=["orders o"],
            )
        ],
        dimensions=[],
    )

    sql = build_sql_from_intent(SemanticIntent(metric="aliased_order_count", limit=10), registry)

    assert "FROM orders o" in sql

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id INTEGER)")
    conn.execute(sql).fetchall()


def test_join_path_resolves_reverse_direction():
    registry = _make_registry(
        joins=[
            SemanticJoin.model_validate(
                {"from": "b", "to": "c", "on": "b.c_id = c.id", "type": "LEFT"}
            ),
            SemanticJoin.model_validate(
                {"from": "a", "to": "b", "on": "a.b_id = b.id", "type": "LEFT"}
            ),
        ],
        metrics=[
            SemanticMetric(
                name="root_count",
                display_name="Root Count",
                description="Count root rows",
                aggregation="COUNT_DISTINCT",
                sql_expression="a.id",
                required_tables=["a"],
            )
        ],
        dimensions=[
            SemanticDimension(
                name="leaf_name",
                display_name="Leaf Name",
                sql_expression="c.name",
                required_tables=["c"],
                cardinality="low",
            )
        ],
    )

    sql = build_sql_from_intent(
        SemanticIntent(metric="root_count", dimensions=["leaf_name"], limit=10),
        registry,
    )

    assert "LEFT JOIN b ON a.b_id = b.id" in sql
    assert "LEFT JOIN c ON b.c_id = c.id" in sql
    assert sql.index("LEFT JOIN b ON a.b_id = b.id") < sql.index("LEFT JOIN c ON b.c_id = c.id")


def test_join_path_picks_shortest_path():
    registry = _make_registry(
        joins=[
            SemanticJoin.model_validate(
                {"from": "a", "to": "b", "on": "a.b_id = b.id", "type": "LEFT"}
            ),
            SemanticJoin.model_validate(
                {"from": "b", "to": "c", "on": "b.c_id = c.id", "type": "LEFT"}
            ),
            SemanticJoin.model_validate(
                {"from": "c", "to": "d", "on": "c.d_id = d.id", "type": "LEFT"}
            ),
            SemanticJoin.model_validate(
                {"from": "a", "to": "d", "on": "a.id = d.a_id", "type": "LEFT"}
            ),
        ],
        metrics=[
            SemanticMetric(
                name="root_count",
                display_name="Root Count",
                description="Count root rows",
                aggregation="COUNT_DISTINCT",
                sql_expression="a.id",
                required_tables=["a"],
            )
        ],
        dimensions=[
            SemanticDimension(
                name="direct_leaf",
                display_name="Direct Leaf",
                sql_expression="d.name",
                required_tables=["d"],
                cardinality="low",
            )
        ],
    )

    sql = build_sql_from_intent(
        SemanticIntent(metric="root_count", dimensions=["direct_leaf"], limit=10),
        registry,
    )

    assert "LEFT JOIN d ON a.id = d.a_id" in sql
    assert "LEFT JOIN b ON a.b_id = b.id" not in sql
    assert "LEFT JOIN c ON b.c_id = c.id" not in sql


# --- Phase 1: physical table references, deterministic ordering, portable date math ---


def test_table_ref_qualifies_from_and_joins_with_logical_alias():
    registry = load_semantic_registry(SCHEMA_PATH)
    intent = SemanticIntent(metric="total_revenue", dimensions=["customer_state"], limit=5)

    sql = build_sql_from_intent(intent, registry, table_ref=lambda t: f"src_{t}.main.{t}")

    assert "FROM src_order_payments.main.order_payments AS op" in sql
    assert "LEFT JOIN src_orders.main.orders AS orders ON" in sql
    assert "LEFT JOIN src_customers.main.customers AS c ON" in sql

    # The qualified SQL executes when each table lives in its own catalog.
    conn = duckdb.connect(":memory:")
    for table, ddl in {
        "orders": "order_id VARCHAR, customer_id VARCHAR, order_status VARCHAR",
        "order_payments": "order_id VARCHAR, payment_value DOUBLE",
        "customers": "customer_id VARCHAR, customer_state VARCHAR",
    }.items():
        conn.execute(f"ATTACH ':memory:' AS src_{table}")
        conn.execute(f"CREATE TABLE src_{table}.main.{table} ({ddl})")
    conn.execute("INSERT INTO src_orders.main.orders VALUES ('o1', 'c1', 'delivered')")
    conn.execute("INSERT INTO src_order_payments.main.order_payments VALUES ('o1', 10.0)")
    conn.execute("INSERT INTO src_customers.main.customers VALUES ('c1', 'SP')")
    assert conn.execute(sql).fetchall() == [("SP", 10.0)]


def test_without_table_ref_output_is_unqualified():
    registry = load_semantic_registry(SCHEMA_PATH)
    sql = build_sql_from_intent(SemanticIntent(metric="total_revenue", dimensions=["customer_state"]), registry)

    assert "FROM order_payments op\n" in sql
    assert "LEFT JOIN orders ON" in sql
    assert " AS orders" not in sql
    assert "LEFT JOIN customers c ON" in sql


def test_table_ref_rejects_unsafe_physical_reference():
    import pytest

    registry = load_semantic_registry(SCHEMA_PATH)
    intent = SemanticIntent(metric="order_count", limit=5)
    for bad in ["orders; DROP TABLE x", "a.b.c.d", "orders --", "'/etc/passwd'", "read_parquet('x')", ""]:
        with pytest.raises(ValueError, match="Invalid physical table reference"):
            build_sql_from_intent(intent, registry, table_ref=lambda _t, bad=bad: bad)


def test_order_by_metric_is_nulls_last_with_output_tie_breakers():
    registry = load_semantic_registry(SCHEMA_PATH)
    intent = SemanticIntent(
        metric="order_count",
        dimensions=["seller_state", "payment_type"],
        time_dimension="order_date",
        order_by="metric_desc",
    )

    sql = build_sql_from_intent(intent, registry)

    assert (
        "ORDER BY metric_value DESC NULLS LAST, seller_state ASC NULLS LAST, "
        "payment_type ASC NULLS LAST, time_bucket ASC NULLS LAST"
    ) in sql


def test_order_by_time_is_nulls_last_with_dimension_tie_breakers():
    registry = load_semantic_registry(SCHEMA_PATH)
    intent = SemanticIntent(
        metric="order_count", dimensions=["seller_state"], time_dimension="order_date", order_by="time_desc"
    )

    sql = build_sql_from_intent(intent, registry)

    assert "ORDER BY time_bucket DESC NULLS LAST, seller_state ASC NULLS LAST" in sql


def test_order_by_time_without_time_dimension_still_raises():
    import pytest

    registry = load_semantic_registry(SCHEMA_PATH)
    with pytest.raises(ValueError, match="requires a time_dimension"):
        build_sql_from_intent(SemanticIntent(metric="order_count", order_by="time_asc"), registry)


def test_null_metric_rows_sort_last_for_desc():
    registry = load_semantic_registry(SCHEMA_PATH)
    sql = build_sql_from_intent(
        SemanticIntent(metric="total_revenue", dimensions=["customer_state"], order_by="metric_desc", limit=10),
        registry,
    )
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, order_status VARCHAR)")
    conn.execute("CREATE TABLE order_payments (order_id VARCHAR, payment_value DOUBLE)")
    conn.execute("CREATE TABLE customers (customer_id VARCHAR, customer_state VARCHAR)")
    conn.execute("INSERT INTO orders VALUES ('o1','c1','delivered'), ('o2','c2','delivered'), ('o3','c3','delivered')")
    conn.execute("INSERT INTO order_payments VALUES ('o1', 5.0), ('o2', 50.0), ('o3', NULL)")  # o3: NULL metric
    conn.execute("INSERT INTO customers VALUES ('c1','RJ'), ('c2','SP'), ('c3','MG')")

    assert conn.execute(sql).fetchall() == [("SP", 50.0), ("RJ", 5.0), ("MG", None)]


def test_average_delivery_days_matches_duckdb_datediff():
    registry = load_semantic_registry(SCHEMA_PATH)
    sql = build_sql_from_intent(SemanticIntent(metric="average_delivery_days"), registry)
    assert "DATEDIFF" not in sql.upper()

    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE orders (order_id VARCHAR, order_status VARCHAR, "
        "order_purchase_timestamp TIMESTAMP, order_delivered_customer_date TIMESTAMP)"
    )
    # Crosses a day boundary after only 2 hours, so elapsed-time math (0.08 days) would differ from 1.
    conn.execute(
        "INSERT INTO orders VALUES "
        "('o1','delivered','2018-01-01 23:00:00','2018-01-02 01:00:00'), "
        "('o2','delivered','2018-01-01 08:00:00','2018-01-11 07:00:00'), "
        "('o3','shipped','2018-01-01 08:00:00', NULL)"
    )
    expected = conn.execute(
        "SELECT AVG(DATEDIFF('day', order_purchase_timestamp, order_delivered_customer_date)) "
        "FROM orders WHERE order_delivered_customer_date IS NOT NULL"
    ).fetchone()[0]
    assert conn.execute(sql).fetchone()[0] == expected == 5.5


@pytest.mark.parametrize("aggregation,expression,expected", [
    ("SUM", "SUM(iris.sepallengthcm)", "SUM(iris.sepallengthcm)"),
    ("SUM", "sum( op.payment_value * 2 )", "SUM(op.payment_value * 2)"),
    ("AVG", "AVG(iris.sepalwidthcm)", "AVG(CAST((iris.sepalwidthcm) AS DOUBLE))"),  # J32: as DOUBLE
    ("COUNT_DISTINCT", "COUNT(DISTINCT iris.species)", "COUNT(DISTINCT iris.species)"),
    ("SUM", "op.payment_value", "SUM(op.payment_value)"),
    # Not a single wrapping call: left alone.
    ("SUM", "SUM(a) / SUM(b)", "SUM(SUM(a) / SUM(b))"),
    ("SUM", "COALESCE(a, 0)", "SUM(COALESCE(a, 0))"),
    ("AVG", "SUM(a)", "AVG(CAST((SUM(a)) AS DOUBLE))"),
])
def test_metric_expression_does_not_nest_its_own_aggregate(aggregation, expression, expected):
    from types import SimpleNamespace

    from app.semantic.sql_builder import _build_metric_expression

    assert _build_metric_expression(SimpleNamespace(aggregation=aggregation, sql_expression=expression)) == expected


def test_time_dimension_table_is_joined_and_keeps_a_declared_alias():
    """A metric that does not need the time dimension's table still gets it joined (generated
    schemas derive minimal required tables); an alias the metric declared for it is kept."""
    from app.semantic import SemanticRegistry
    from app.semantic.models import SemanticSchema

    base = load_semantic_registry(SCHEMA_PATH).schema
    payments_only = base.model_copy(update={"metrics": [
        m.model_copy(update={"name": "payments_sum", "sql_expression": "order_payments.payment_value",
                             "required_tables": ["order_payments"], "base_filter": None})
        for m in base.metrics if m.name == "total_revenue"
    ] + [
        m.model_copy(update={"name": "aliased", "sql_expression": "o.order_id", "aggregation": "COUNT_DISTINCT",
                             "required_tables": ["orders o"], "base_filter": None})
        for m in base.metrics if m.name == "order_count"
    ]})
    registry = SemanticRegistry(SemanticSchema.model_validate(payments_only.model_dump(by_alias=True)))
    trend = build_sql_from_intent(SemanticIntent(metric="payments_sum", time_dimension="order_date",
                                                 time_granularity="month"), registry)
    assert "JOIN orders" in trend and "DATE_TRUNC('month', orders.order_purchase_timestamp)" in trend
    dated = build_sql_from_intent(SemanticIntent(metric="payments_sum", start_date="2018-01-01"), registry)
    assert "JOIN orders" in dated
    aliased = build_sql_from_intent(SemanticIntent(metric="aliased", time_dimension="order_date",
                                                   time_granularity="month"), registry)
    assert "FROM orders o" in aliased and "o.order_purchase_timestamp" in aliased
