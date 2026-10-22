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


# An allowlist entry is either a dotted name ("orders", "src.public.orders") or a tuple of
# identifier parts, needed when a part itself contains a dot (a quoted "jammi.result" name).
AllowedTable = str | tuple[str, ...]
TableKey = tuple[str, ...]


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
    exp.Copy,
    exp.Set,
)


def _extract_limit(statement: exp.Expression) -> int | None:
    limit_expr = statement.args.get("limit")
    if not limit_expr:
        return None

    limit_value = limit_expr.expression
    if isinstance(limit_value, exp.Literal) and limit_value.is_int:
        return int(limit_value.this)
    return None


def _identifier_part(node: exp.Expression | None) -> str:
    if not isinstance(node, exp.Identifier):
        return ""
    # Unquoted identifiers are case-insensitive (folded to lowercase by DataFusion and DuckDB).
    return node.this if node.quoted else node.this.lower()


def _table_key(table: exp.Table) -> TableKey:
    parts = (
        _identifier_part(table.args.get("catalog")),
        _identifier_part(table.args.get("db")),
        _identifier_part(table.this),
    )
    return tuple(part for part in parts if part)


def _allowed_key(entry: AllowedTable) -> TableKey:
    if isinstance(entry, tuple):
        return entry
    return tuple(part.lower() for part in entry.split("."))


def _visible_ctes(table: exp.Table) -> set[str]:
    """The CTE names in scope at a table reference: those of every WITH on an enclosing query. A
    CTE is visible inside the query it is attached to (its body, later CTEs, itself if recursive),
    never outside it. Collecting names from the whole statement exempted an outer physical `users`
    because a nested subquery defined a CTE called users (review B7)."""
    # Inside a CTE's own definition only the CTEs declared before it are visible, and itself only
    # under WITH RECURSIVE: `WITH users AS (SELECT * FROM users)` reads the physical users table
    # (second review: every enclosing CTE name was treated as visible, so that escaped the allowlist).
    names: set[str] = set()
    inside: exp.CTE | None = None  # the CTE whose definition the walk came up through
    node = table.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            inside = node
        with_clause = node.args.get("with_") or node.args.get("with")
        if isinstance(with_clause, exp.With):
            ctes = list(with_clause.expressions)
            if inside is not None and any(cte is inside for cte in ctes):
                position = next(i for i, cte in enumerate(ctes) if cte is inside)
                visible = ctes[:position + 1] if with_clause.args.get("recursive") else ctes[:position]
            else:
                visible = ctes  # the query's body sees every CTE of its WITH
            names |= {cte.alias_or_name.lower() for cte in visible if cte.alias_or_name}
            inside = None
        node = node.parent
    return names


def _table_refs(statement: exp.Expression) -> list[exp.Table]:
    """Every physical table reference, excluding references to CTEs defined in the query.

    Raises on table-valued functions (e.g. read_parquet(...), annotate(...)): they read
    files or run models and never resolve to an allowlisted table.
    """
    refs: list[exp.Table] = []
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SQLSafetyError("Table functions are not allowed in queries")
        key = _table_key(table)
        if len(key) == 1 and key[0].lower() in _visible_ctes(table):
            continue
        refs.append(table)
    return refs


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
    """Returns (has unqualified *, the table names or aliases used as <name>.*)."""
    has_unqualified_star = False
    qualified_star_tables: set[str] = set()

    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            projection_sql = projection.sql(dialect=dialect).strip()
            if projection_sql == "*":
                has_unqualified_star = True
            elif projection_sql.endswith(".*"):
                qualified_star_tables.add(projection_sql[:-2].split(".")[-1].strip('"`[]').lower())

    return has_unqualified_star, qualified_star_tables


def _columns_for(table: exp.Table, table_columns: dict[str, set[str]]) -> set[str]:
    key = _table_key(table)
    return table_columns.get(".".join(key)) or table_columns.get(key[-1], set())


def validate_sql_safety(
    sql: str,
    *,
    allowed_tables: set[AllowedTable],
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

    if not isinstance(statement, (exp.Select, exp.SetOperation)):
        raise SQLSafetyError("Query must be a SELECT statement")

    # Exact match on every identifier part: a bare allowlist entry ("orders") only matches an
    # unqualified reference, never other_source.public.orders.
    refs = _table_refs(statement)
    allowed_keys = {_allowed_key(entry) for entry in allowed_tables}
    tables = sorted({".".join(_table_key(table)) for table in refs})
    disallowed = sorted({".".join(_table_key(t)) for t in refs if _table_key(t) not in allowed_keys})
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
        has_unqualified_star, qualified_star_names = _extract_star_tables(statement, dialect=dialect)
        star_refs = [
            table
            for table in refs
            if has_unqualified_star
            or table.alias_or_name.lower() in qualified_star_names
            or _table_key(table)[-1] in qualified_star_names
        ]
        denied_via_star = sorted(
            {
                ".".join(_table_key(table))
                for table in star_refs
                if effective_denied.intersection(_columns_for(table, table_columns))
            }
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
