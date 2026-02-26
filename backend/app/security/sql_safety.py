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
    return sorted({table.name for table in statement.find_all(exp.Table) if table.name})


def validate_sql_safety(
    sql: str,
    *,
    allowed_tables: set[str],
    max_limit: int = 5000,
) -> SQLSafetyResult:
    if not sql or not sql.strip():
        raise SQLSafetyError("SQL is empty")

    try:
        parsed = sqlglot.parse(sql, read="duckdb")
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

    limit = _extract_limit(statement)
    if limit is None:
        raise SQLSafetyError("Query must include an integer LIMIT clause")
    if limit <= 0:
        raise SQLSafetyError("LIMIT must be greater than 0")
    if limit > max_limit:
        raise SQLSafetyError(f"LIMIT {limit} exceeds maximum allowed {max_limit}")

    return SQLSafetyResult(tables=tables, limit=limit)
