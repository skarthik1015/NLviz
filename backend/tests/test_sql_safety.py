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
