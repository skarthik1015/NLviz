import pytest

from app.security import SQLSafetyError, validate_sql_safety


ALLOWED_TABLES = {
    "orders",
    "order_items",
    "order_payments",
    "order_reviews",
    "customers",
    "sellers",
    "products",
    "category_translation",
}

TABLE_COLUMNS = {
    "orders": {"order_id", "customer_id", "order_status"},
    "order_items": {"order_id", "product_id", "seller_id"},
    "order_payments": {"order_id", "payment_type", "payment_value"},
    "order_reviews": {"order_id", "review_score"},
    "customers": {"customer_id", "customer_state", "customer_unique_id"},
    "sellers": {"seller_id", "seller_state"},
    "products": {"product_id", "product_category_name_english"},
    "category_translation": {"product_category_name", "product_category_name_english"},
}


def test_allows_basic_select_query():
    result = validate_sql_safety(
        "SELECT COUNT(*) FROM orders LIMIT 100",
        allowed_tables=ALLOWED_TABLES,
        table_columns=TABLE_COLUMNS,
        max_limit=5000,
    )
    assert result.tables == ["orders"]
    assert result.limit == 100


def test_blocks_non_select_query():
    with pytest.raises(SQLSafetyError, match="read-only SELECT"):
        validate_sql_safety(
            "UPDATE orders SET order_status = 'x' LIMIT 1",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_blocks_disallowed_table():
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety(
            "SELECT * FROM users LIMIT 10",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_blocks_multiple_statements():
    with pytest.raises(SQLSafetyError, match="one SQL statement"):
        validate_sql_safety(
            "SELECT * FROM orders LIMIT 10; SELECT * FROM customers LIMIT 10;",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_blocks_missing_limit():
    with pytest.raises(SQLSafetyError, match="LIMIT"):
        validate_sql_safety(
            "SELECT * FROM orders",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_blocks_limit_above_max():
    with pytest.raises(SQLSafetyError, match="exceeds maximum"):
        validate_sql_safety(
            "SELECT * FROM orders LIMIT 6000",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_allows_cte_backed_by_allowlisted_tables():
    result = validate_sql_safety(
        """
        WITH recent_orders AS (
            SELECT order_id
            FROM orders
            LIMIT 5
        )
        SELECT *
        FROM recent_orders
        LIMIT 5
        """,
        allowed_tables=ALLOWED_TABLES,
        table_columns=TABLE_COLUMNS,
        max_limit=5000,
    )
    assert result.tables == ["orders"]
    assert result.limit == 5


def test_blocks_disallowed_table_inside_subquery():
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety(
            """
            SELECT *
            FROM (
                SELECT *
                FROM users
                LIMIT 5
            ) suspicious
            LIMIT 5
            """,
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_blocks_non_integer_limit():
    with pytest.raises(SQLSafetyError, match="integer LIMIT"):
        validate_sql_safety(
            "SELECT * FROM orders LIMIT '10'",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_denied_column_rejected():
    with pytest.raises(SQLSafetyError, match="denied columns: customer_unique_id"):
        validate_sql_safety(
            "SELECT customer_unique_id FROM customers LIMIT 10",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_allowed_columns_pass():
    result = validate_sql_safety(
        "SELECT customer_id, customer_state FROM customers LIMIT 10",
        allowed_tables=ALLOWED_TABLES,
        table_columns=TABLE_COLUMNS,
        max_limit=5000,
    )
    assert result.tables == ["customers"]
    assert result.limit == 10


def test_denied_column_rejected_via_wildcard():
    with pytest.raises(SQLSafetyError, match="wildcard"):
        validate_sql_safety(
            "SELECT * FROM customers LIMIT 10",
            allowed_tables=ALLOWED_TABLES,
            table_columns=TABLE_COLUMNS,
            max_limit=5000,
        )


def test_qualified_wildcard_for_safe_table_passes():
    result = validate_sql_safety(
        "SELECT orders.* FROM orders JOIN customers ON orders.customer_id = customers.customer_id LIMIT 10",
        allowed_tables=ALLOWED_TABLES,
        table_columns=TABLE_COLUMNS,
        max_limit=5000,
    )
    assert result.tables == ["customers", "orders"]


# --- Phase 1: workspace isolation on qualified names, table functions, non-SELECT statements ---

WS_A = {
    "wa_c1_orders.public.orders",
    "wa_c1_customers.public.customers",
}
WS_A_COLUMNS = {
    "wa_c1_orders.public.orders": {"order_id", "customer_id"},
    "wa_c1_customers.public.customers": {"customer_id", "customer_state", "customer_unique_id"},
}


def test_qualified_reference_in_allowlist_passes():
    result = validate_sql_safety(
        "SELECT c.customer_state, COUNT(*) AS n FROM wa_c1_orders.public.orders AS orders "
        "JOIN wa_c1_customers.public.customers AS c ON orders.customer_id = c.customer_id "
        "GROUP BY c.customer_state LIMIT 10",
        allowed_tables=WS_A,
    )
    assert result.tables == ["wa_c1_customers.public.customers", "wa_c1_orders.public.orders"]


def test_other_workspace_table_with_same_bare_name_is_rejected():
    with pytest.raises(SQLSafetyError, match="outside allowlist: wb_c9_orders.public.orders"):
        validate_sql_safety("SELECT COUNT(*) FROM wb_c9_orders.public.orders LIMIT 10", allowed_tables=WS_A)


def test_bare_allowlist_entry_does_not_match_qualified_reference():
    with pytest.raises(SQLSafetyError, match="outside allowlist: other_ws.public.orders"):
        validate_sql_safety("SELECT COUNT(*) FROM other_ws.public.orders LIMIT 10", allowed_tables={"orders"})


def test_partially_qualified_reference_does_not_match_full_entry():
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety("SELECT COUNT(*) FROM public.orders LIMIT 10", allowed_tables=WS_A)


def test_unquoted_identifiers_match_case_insensitively():
    result = validate_sql_safety("SELECT COUNT(*) FROM WA_C1_Orders.PUBLIC.Orders LIMIT 10", allowed_tables=WS_A)
    assert result.tables == ["wa_c1_orders.public.orders"]


def test_quoted_name_containing_a_dot_needs_a_tuple_entry():
    sql = 'SELECT COUNT(*) FROM "jammi.reviews_sentiment" LIMIT 10'
    assert validate_sql_safety(sql, allowed_tables={("jammi.reviews_sentiment",)}).limit == 10
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety(sql, allowed_tables={"jammi.reviews_sentiment"})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('/etc/x.parquet') LIMIT 5",
        "SELECT * FROM annotate('m', 'classification', 'wa_c1_orders.public.orders', 'id', 'txt') LIMIT 5",
        "SELECT n FROM (SELECT COUNT(*) AS n FROM read_csv('/etc/passwd')) s LIMIT 5",
        "SELECT o.order_id FROM wa_c1_orders.public.orders AS o "
        "JOIN annotate('m', 'ner', 'x.public.y', 'id', 't') a ON a._row_id = o.order_id LIMIT 5",
    ],
)
def test_table_functions_are_rejected(sql):
    with pytest.raises(SQLSafetyError, match="Table functions are not allowed"):
        validate_sql_safety(sql, allowed_tables=WS_A)


@pytest.mark.parametrize(
    "sql",
    [
        "COPY (SELECT order_id FROM wa_c1_orders.public.orders) TO '/tmp/x.parquet'",
        "SET datafusion.catalog.information_schema = true",
        "CREATE TABLE t AS SELECT order_id FROM wa_c1_orders.public.orders LIMIT 5",
        "DROP TABLE wa_c1_orders.public.orders",
        "DESCRIBE wa_c1_orders.public.orders",
    ],
)
def test_non_select_statements_are_rejected(sql):
    with pytest.raises(SQLSafetyError):
        validate_sql_safety(sql, allowed_tables=WS_A)


def test_information_schema_is_rejected_unless_allowlisted():
    with pytest.raises(SQLSafetyError, match="outside allowlist: information_schema.tables"):
        validate_sql_safety("SELECT table_name FROM information_schema.tables LIMIT 50", allowed_tables=WS_A)


def test_union_checks_tables_on_both_sides():
    with pytest.raises(SQLSafetyError, match="outside allowlist: wb_c9_orders.public.orders"):
        validate_sql_safety(
            "SELECT order_id FROM wa_c1_orders.public.orders UNION ALL "
            "SELECT order_id FROM wb_c9_orders.public.orders LIMIT 10",
            allowed_tables=WS_A,
        )


def test_denied_column_via_alias_wildcard_on_qualified_table():
    with pytest.raises(SQLSafetyError, match="wildcard from tables: wa_c1_customers.public.customers"):
        validate_sql_safety(
            "SELECT c.* FROM wa_c1_customers.public.customers AS c LIMIT 10",
            allowed_tables=WS_A,
            table_columns=WS_A_COLUMNS,
        )


def test_cte_named_like_a_table_does_not_shadow_a_qualified_reference():
    with pytest.raises(SQLSafetyError, match="outside allowlist: wb_c9_orders.public.orders"):
        validate_sql_safety(
            "WITH orders AS (SELECT order_id FROM wa_c1_orders.public.orders LIMIT 5) "
            "SELECT o.order_id FROM orders o JOIN wb_c9_orders.public.orders x ON x.order_id = o.order_id LIMIT 5",
            allowed_tables=WS_A,
        )


def test_every_compiled_golden_intent_passes_only_its_own_workspace_allowlist():
    """Compiler output with a workspace resolver passes that workspace's allowlist and fails another's."""
    from pathlib import Path

    from app.models import SemanticIntent
    from app.semantic import build_sql_from_intent, load_semantic_registry
    from golden_tests import GOLDEN_CASES  # tests/ is on sys.path under pytest

    registry = load_semantic_registry(
        Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
    )
    tables = [t.name for t in registry.schema.tables]

    def resolver(workspace):
        return lambda table: f"{workspace}_{table}.public.{table}"

    own = {resolver("wa")(t) for t in tables}
    other = {resolver("wb")(t) for t in tables}
    for case in GOLDEN_CASES:
        intent = SemanticIntent(
            metric=case.metric,
            dimensions=list(case.dimensions),
            time_dimension=case.time_dimension,
            order_by=case.order_by,
        )
        sql = build_sql_from_intent(intent, registry, table_ref=resolver("wa"))
        assert validate_sql_safety(sql, allowed_tables=own).tables, case.question
        with pytest.raises(SQLSafetyError, match="outside allowlist"):
            validate_sql_safety(sql, allowed_tables=other)


# Review B7: a CTE is visible only inside the query it is attached to.
@pytest.mark.parametrize("sql", [
    "SELECT * FROM users WHERE EXISTS (WITH users AS (SELECT 1 AS x) SELECT x FROM users) LIMIT 1",
    'SELECT * FROM "jammi.other_result" WHERE EXISTS '
    '(WITH "jammi.other_result" AS (SELECT 1 AS x) SELECT x FROM "jammi.other_result") LIMIT 1',
    "SELECT order_id FROM orders UNION ALL "
    "SELECT * FROM (WITH users AS (SELECT 1 AS x) SELECT x FROM users) t CROSS JOIN users LIMIT 1",
])
def test_a_nested_cte_does_not_hide_an_outer_table(sql):
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety(sql, allowed_tables={"orders"})


def test_ctes_still_resolve_in_their_own_scope():
    validate_sql_safety(
        "WITH a AS (SELECT order_id FROM orders), b AS (SELECT order_id FROM a) "
        "SELECT order_id FROM b WHERE EXISTS (SELECT 1 FROM a) LIMIT 5",
        allowed_tables={"orders"},
    )
    validate_sql_safety(
        "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i FROM n LIMIT 5",
        allowed_tables={"orders"},
    )


# Second review: inside a CTE's definition, only earlier CTEs (and itself if recursive) are in scope.
@pytest.mark.parametrize("sql", [
    "WITH users AS (SELECT * FROM users) SELECT * FROM users LIMIT 1",
    "WITH a AS (SELECT * FROM b), b AS (SELECT order_id FROM orders) SELECT * FROM a LIMIT 1",
    "SELECT * FROM (WITH users AS (SELECT * FROM users) SELECT * FROM users) t LIMIT 1",
])
def test_a_cte_body_does_not_see_itself_or_later_ctes(sql):
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety(sql, allowed_tables={"orders"})


def test_later_ctes_see_earlier_ones():
    validate_sql_safety("WITH a AS (SELECT order_id FROM orders), b AS (SELECT order_id FROM a) "
                        "SELECT order_id FROM b LIMIT 5", allowed_tables={"orders"})
