from pathlib import Path

import duckdb

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
