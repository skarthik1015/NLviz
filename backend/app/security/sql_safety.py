from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class SQLSafetyError(ValueError):
    pass


@dataclass
class SQLSafetyResult:
    tables: list[str]
    limit: int


DENIED_COLUMNS = {
    "customer_unique_id",
}


_BLOCKED_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Merge,
)


def _extract_limit(statement: exp.Expression) -> int | None:
    limit_expr = statement.args.get("limit")
    if not limit_expr:
        return None

    limit_value = limit_expr.expression
    if isinstance(limit_value, exp.Literal) and limit_value.is_int:
        return int(limit_value.this)
    return None


def _extract_tables(statement: exp.Expression) -> list[str]:
    cte_aliases = {
        cte.alias_or_name
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    return sorted(
        {
            table.name
            for table in statement.find_all(exp.Table)
            if table.name and table.name not in cte_aliases
        }
    )


def _extract_selected_columns(statement: exp.Expression) -> list[str]:
    selected_columns: set[str] = set()
    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            for column in projection.find_all(exp.Column):
                if column.name:
                    selected_columns.add(column.name)
    return sorted(selected_columns)


def _extract_star_tables(
    statement: exp.Expression, dialect: str = "duckdb"
) -> tuple[bool, set[str]]:
    has_unqualified_star = False
    qualified_star_tables: set[str] = set()

    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            projection_sql = projection.sql(dialect=dialect).strip()
            if projection_sql == "*":
                has_unqualified_star = True
            elif projection_sql.endswith(".*"):
                qualified_star_tables.add(projection_sql[:-2].split(".")[-1].strip('"`[]'))

    return has_unqualified_star, qualified_star_tables


def validate_sql_safety(
    sql: str,
    *,
    allowed_tables: set[str],
    table_columns: dict[str, set[str]] | None = None,
    max_limit: int = 5000,
    denied_columns: frozenset[str] | None = None,
    dialect: str = "duckdb",
) -> SQLSafetyResult:
    if not sql or not sql.strip():
        raise SQLSafetyError("SQL is empty")

    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except Exception as exc:
        raise SQLSafetyError(f"SQL parse failed: {exc}") from exc

    if len(parsed) != 1:
        raise SQLSafetyError("Only one SQL statement is allowed")

    statement = parsed[0]

    if any(isinstance(node, _BLOCKED_NODE_TYPES) for node in statement.walk()):
        raise SQLSafetyError("Only read-only SELECT queries are allowed")

    if not any(isinstance(node, exp.Select) for node in statement.walk()):
        raise SQLSafetyError("Query must contain a SELECT statement")

    tables = _extract_tables(statement)
    disallowed = sorted(set(tables) - set(allowed_tables))
    if disallowed:
        raise SQLSafetyError(
            f"Query references tables outside allowlist: {', '.join(disallowed)}"
        )

    effective_denied = denied_columns if denied_columns is not None else frozenset(DENIED_COLUMNS)
    matched_denied = sorted(set(_extract_selected_columns(statement)) & effective_denied)
    if matched_denied:
        raise SQLSafetyError(
            f"Query selects denied columns: {', '.join(matched_denied)}"
        )

    if table_columns:
        has_unqualified_star, qualified_star_tables = _extract_star_tables(statement, dialect=dialect)
        star_tables = tables if has_unqualified_star else sorted(qualified_star_tables)
        denied_via_star = sorted(
            table_name
            for table_name in star_tables
            if effective_denied.intersection(table_columns.get(table_name, set()))
        )
        if denied_via_star:
            raise SQLSafetyError(
                "Query selects denied columns via wildcard from tables: "
                + ", ".join(denied_via_star)
            )

    limit = _extract_limit(statement)
    if limit is None:
        raise SQLSafetyError("Query must include an integer LIMIT clause")
    if limit <= 0:
        raise SQLSafetyError("LIMIT must be greater than 0")
    if limit > max_limit:
        raise SQLSafetyError(f"LIMIT {limit} exceeds maximum allowed {max_limit}")

    return SQLSafetyResult(tables=tables, limit=limit)
