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


def test_allows_basic_select_query():
    result = validate_sql_safety(
        "SELECT COUNT(*) FROM orders LIMIT 100",
        allowed_tables=ALLOWED_TABLES,
        max_limit=5000,
    )
    assert result.tables == ["orders"]
    assert result.limit == 100


def test_blocks_non_select_query():
    with pytest.raises(SQLSafetyError, match="read-only SELECT"):
        validate_sql_safety(
            "UPDATE orders SET order_status = 'x' LIMIT 1",
            allowed_tables=ALLOWED_TABLES,
            max_limit=5000,
        )


def test_blocks_disallowed_table():
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        validate_sql_safety(
            "SELECT * FROM users LIMIT 10",
            allowed_tables=ALLOWED_TABLES,
            max_limit=5000,
        )


def test_blocks_multiple_statements():
    with pytest.raises(SQLSafetyError, match="one SQL statement"):
        validate_sql_safety(
            "SELECT * FROM orders LIMIT 10; SELECT * FROM customers LIMIT 10;",
            allowed_tables=ALLOWED_TABLES,
            max_limit=5000,
        )


def test_blocks_missing_limit():
    with pytest.raises(SQLSafetyError, match="LIMIT"):
        validate_sql_safety(
            "SELECT * FROM orders",
            allowed_tables=ALLOWED_TABLES,
            max_limit=5000,
        )


def test_blocks_limit_above_max():
    with pytest.raises(SQLSafetyError, match="exceeds maximum"):
        validate_sql_safety(
            "SELECT * FROM orders LIMIT 6000",
            allowed_tables=ALLOWED_TABLES,
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
            max_limit=5000,
        )


def test_blocks_non_integer_limit():
    with pytest.raises(SQLSafetyError, match="integer LIMIT"):
        validate_sql_safety(
            "SELECT * FROM orders LIMIT '10'",
            allowed_tables=ALLOWED_TABLES,
            max_limit=5000,
        )
