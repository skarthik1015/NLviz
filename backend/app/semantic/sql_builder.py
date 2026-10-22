from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timedelta
import re

from app.models.semantic_intent import FilterCondition, SemanticIntent

from .loader import SemanticRegistry
from .models import SemanticJoin

# Maps a logical table name from the semantic schema to the physical reference the
# engine resolves (e.g. "orders" -> "w1a2_c3b4_orders.public.orders" on Jammi).
TableRefResolver = Callable[[str], str]

_PHYSICAL_TABLE_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}")


def _parse_table_ref(table_ref: str) -> tuple[str, str]:
    parts = table_ref.split()
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"Invalid table reference: {table_ref}")


def _add_required_tables(required: set[str], aliases: dict[str, str], table_refs: list[str]) -> None:
    for table_ref in table_refs:
        base_table, alias = _parse_table_ref(table_ref)
        required.add(base_table)
        aliases[base_table] = alias


def _apply_aliases(expression: str, aliases: dict[str, str]) -> str:
    updated = expression
    for table_name, alias in aliases.items():
        updated = re.sub(rf"\b{table_name}\.", f"{alias}.", updated)
    return updated


def _format_table_with_alias(table_name: str, alias: str, table_ref: TableRefResolver | None = None) -> str:
    if table_ref is None:
        if alias == table_name:
            return table_name
        return f"{table_name} {alias}"
    physical = table_ref(table_name)
    if not _PHYSICAL_TABLE_REF.fullmatch(physical):
        raise ValueError(f"Invalid physical table reference for '{table_name}': {physical!r}")
    # Always alias, so expressions written against the logical name (orders.col) still resolve.
    return f"{physical} AS {alias}"


def _order_by_clause(order_by: str, *, has_time: bool, tie_breakers: list[str]) -> str:
    """ORDER BY with explicit NULLS LAST (DuckDB's default; DataFusion sorts NULLs first on DESC)
    and the output columns as tie-breakers, so LIMIT cuts the same rows on every engine."""
    if order_by in {"metric_desc", "metric_asc"}:
        direction = "DESC" if order_by == "metric_desc" else "ASC"
        keys = [f"metric_value {direction} NULLS LAST"]
        tie_columns = tie_breakers + (["time_bucket"] if has_time else [])
    elif order_by in {"time_asc", "time_desc"}:
        if not has_time:
            raise ValueError(f"order_by='{order_by}' requires a time_dimension")
        direction = "ASC" if order_by == "time_asc" else "DESC"
        keys = [f"time_bucket {direction} NULLS LAST"]
        tie_columns = tie_breakers
    else:
        raise ValueError(f"Unsupported order_by option: {order_by}")
    keys.extend(f"{column} ASC NULLS LAST" for column in tie_columns)
    return "ORDER BY " + ", ".join(keys)


_AGGREGATE_CALL = re.compile(r"^\s*(SUM|AVG|COUNT)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)


def _strip_aggregate(expression: str, aggregation: str) -> str:
    """Unwrap `SUM(x)` when the metric's aggregation is already SUM (and likewise AVG, COUNT,
    `COUNT(DISTINCT x)`). Small LLMs often repeat the aggregate inside sql_expression, which would
    compile to a nested aggregate: DuckDB rejects it, DataFusion fails with "Physical plan does not
    support logical expression AggregateFunction"."""
    match = _AGGREGATE_CALL.match(expression)
    if not match:
        return expression
    function, inner = match.group(1).upper(), match.group(2)
    depth = 0
    for char in inner:  # the outer parentheses must enclose the whole expression
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            return expression
    if depth != 0:
        return expression
    distinct = re.match(r"^\s*DISTINCT\s+(.*)$", inner, re.IGNORECASE | re.DOTALL)
    if aggregation == "COUNT_DISTINCT" and function == "COUNT" and distinct:
        return distinct.group(1).strip()
    if aggregation == function and not distinct:
        return inner.strip()
    return expression


def _build_metric_expression(metric) -> str:
    if metric.aggregation == "AVG":
        # As DOUBLE: Jammi fails converting a federated Postgres AVG over NUMERIC to Arrow (the
        # result has more decimal places than the column's scale; issue J32).
        return f"AVG(CAST(({_strip_aggregate(metric.sql_expression, 'AVG')}) AS DOUBLE))"
    if metric.aggregation in {"SUM", "COUNT"}:
        return f"{metric.aggregation}({_strip_aggregate(metric.sql_expression, metric.aggregation)})"
    if metric.aggregation == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {_strip_aggregate(metric.sql_expression, 'COUNT_DISTINCT')})"
    if metric.aggregation == "RATIO":
        numerator, denominator = _aggregated(metric.numerator_sql), _aggregated(metric.denominator_sql)
        return f"({numerator})::DOUBLE / NULLIF(({denominator}), 0)"
    raise ValueError(f"Unsupported aggregation: {metric.aggregation}")


_HAS_AGGREGATE = re.compile(r"\b(SUM|AVG|COUNT|MIN|MAX)\s*\(", re.IGNORECASE)


def ratio_part_is_distinct_count(expression: str | None) -> bool:
    """`COUNT(DISTINCT ...)`: a ratio of two of these is not inflated by a one-to-many join."""
    return bool(expression) and re.match(r"^\s*COUNT\s*\(\s*DISTINCT\b", expression, re.IGNORECASE) is not None


def _aggregated(expression: str) -> str:
    """A ratio part as an aggregate. Generated schemas give row values ("CASE WHEN status =
    'cancelled' THEN 1 ELSE 0 END" over "1"), hand-written ones aggregates ("COUNT(...)"); a row
    value divided as is gave each row's 1 or 0 instead of the share (review B18). Summed here, so
    schemas published before this fix compile correctly too."""
    return expression if _HAS_AGGREGATE.search(expression) else f"SUM({expression})"


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _next_day(value) -> str | None:
    """The day after a calendar-day value, as a literal; None for anything else. An end date is a
    whole day: `<= '2018-12-31'` on a timestamp keeps only midnight of the last day (review B5), so
    an inclusive end compiles to `< '2019-01-01'`."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        day = value
    elif isinstance(value, str) and _ISO_DAY.match(value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return _to_sql_literal(day + timedelta(days=1))


def _to_sql_literal(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def items_expression(expression: str, separator: str) -> str:
    """The items of a delimited list value as an array: 'French, Japanese' -> ['French', 'Japanese']
    (spaces around the separator removed). DataFusion: string_to_array + regexp_replace."""
    pattern = (r"\s*" + re.escape(separator) + r"\s*").replace("'", "''")
    literal = separator.replace("'", "''")
    return f"string_to_array(regexp_replace({expression}, '{pattern}', '{literal}', 'g'), '{literal}')"


def _build_list_filter_clause(filter_condition: FilterCondition, expression: str, separator: str) -> str | None:
    """A filter on a list dimension matches whole items: cuisine = 'Chinese' must match
    'Chinese, Thai' (a plain = missed 2,379 of 2,733 restaurants) and must not match 'Indo-Chinese'.
    None: the operator compares the raw text (contains, ranges)."""
    operator, value = filter_condition.operator, filter_condition.value
    items = items_expression(expression, separator)
    if operator in {"eq", "ne"}:
        clause = f"array_has({items}, {_to_sql_literal(value)})"
    elif operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value:
            raise ValueError(f"Filter operator '{operator}' requires a non-empty list")
        clause = f"array_has_any({items}, make_array({', '.join(_to_sql_literal(v) for v in value)}))"
    else:
        return None
    return f"NOT {clause}" if operator in {"ne", "not_in"} else clause


def _build_filter_clause(filter_condition: FilterCondition, expression: str, separator: str | None = None) -> str:
    operator = filter_condition.operator
    value = filter_condition.value

    if operator == "is_null":
        return f"{expression} IS NULL"
    if operator == "is_not_null":
        return f"{expression} IS NOT NULL"
    if separator:
        clause = _build_list_filter_clause(filter_condition, expression, separator)
        if clause is not None:
            return clause
    if operator == "eq":
        return f"{expression} = {_to_sql_literal(value)}"
    if operator == "ne":
        return f"{expression} <> {_to_sql_literal(value)}"
    if operator == "gt":
        # A whole day: "after 2018-12-31" starts at 2019-01-01 00:00 (a timestamp at noon that day is not after it).
        following = _next_day(value)
        return f"{expression} >= {following}" if following else f"{expression} > {_to_sql_literal(value)}"
    if operator == "gte":
        return f"{expression} >= {_to_sql_literal(value)}"
    if operator == "lt":
        return f"{expression} < {_to_sql_literal(value)}"
    if operator == "lte":
        following = _next_day(value)
        return f"{expression} < {following}" if following else f"{expression} <= {_to_sql_literal(value)}"
    if operator == "contains":
        return f"{expression} ILIKE '%' || {_to_sql_literal(value)} || '%'"
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value:
            raise ValueError(f"Filter operator '{operator}' requires a non-empty list")
        values_sql = ", ".join(_to_sql_literal(v) for v in value)
        keyword = "IN" if operator == "in" else "NOT IN"
        return f"{expression} {keyword} ({values_sql})"
    if operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("Filter operator 'between' requires exactly two values")
        following = _next_day(value[1])
        if following:
            return f"{expression} >= {_to_sql_literal(value[0])} AND {expression} < {following}"
        return f"{expression} BETWEEN {_to_sql_literal(value[0])} AND {_to_sql_literal(value[1])}"
    raise ValueError(f"Unsupported filter operator: {operator}")


def _time_bucket_expression(time_expr: str, granularity: str) -> str:
    return f"DATE_TRUNC('{granularity}', {time_expr})"


def _join_key(join: SemanticJoin) -> tuple[str, str, str, str]:
    return (join.from_table, join.to_table, join.on, join.join_type)


def _build_join_graph(registry: SemanticRegistry) -> dict[str, list[tuple[str, SemanticJoin]]]:
    graph: dict[str, list[tuple[str, SemanticJoin]]] = {}
    for join in registry.list_joins():
        graph.setdefault(join.from_table, []).append((join.to_table, join))
        graph.setdefault(join.to_table, []).append((join.from_table, join))

    for edges in graph.values():
        edges.sort(key=lambda edge: (edge[0],) + _join_key(edge[1]))
    return graph


def _find_join_path(
    base_table: str,
    target_table: str,
    graph: dict[str, list[tuple[str, SemanticJoin]]],
) -> list[SemanticJoin] | None:
    queue: deque[tuple[str, list[SemanticJoin]]] = deque([(base_table, [])])
    visited = {base_table}

    while queue:
        current_table, path = queue.popleft()
        if current_table == target_table:
            return path

        for neighbor, join in graph.get(current_table, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, path + [join]))

    return None


def fan_out_joins(metric_name: str, tables: set[str], registry: SemanticRegistry) -> list[tuple[str, str]]:
    """(one table, many table) for each join the metric's rows would be repeated across: summing
    invoice totals by genre walks invoice -> invoice_line, so each invoice counts once per line.
    Only joins whose relationship is known are checked; a COUNT DISTINCT is never inflated, nor a
    ratio of two of them. Other ratios are: payments per order summed over order items is too high."""
    metric = registry.get_metric(metric_name)
    if not metric.required_tables or metric.aggregation == "COUNT_DISTINCT":
        return []
    if metric.aggregation == "RATIO" and ratio_part_is_distinct_count(metric.numerator_sql) \
            and ratio_part_is_distinct_count(metric.denominator_sql):
        return []
    return fanned_tables(metric.required_tables[0].split()[0], tables, registry)


def fanned_tables(base: str, tables: set[str], registry: SemanticRegistry) -> list[tuple[str, str]]:
    """(one table, many table) for each join from `base` to `tables` that repeats `base`'s rows.
    Shared by the compiler's fan-out check and the SQL checks (app.services.sql_checks)."""
    graph = _build_join_graph(registry)
    fanned: list[tuple[str, str]] = []
    links = {t.name for t in registry.schema.tables if getattr(t, "link", False)}
    for target in sorted(tables - {base}):
        path = _find_join_path(base, target, graph) or []
        current = base
        for position, join in enumerate(path):
            nxt = join.to_table if join.from_table == current else join.from_table
            if join.relationship == "many_to_one" and nxt == join.from_table and (current, nxt) not in fanned:
                # Into a link table and straight out by its other key (film -> film_category ->
                # category): each film once per category, so nothing repeats within a group.
                following = path[position + 1] if position + 1 < len(path) else None
                through_link = nxt in links and following is not None and following.from_table == nxt
                if not through_link:
                    fanned.append((current, nxt))
            current = nxt
    return fanned


def _average_grain_keys(metric, tables: set[str], registry: SemanticRegistry, aliases: dict[str, str]) -> list[str]:
    """The key columns of the "one" side of each join an average would repeat across (read from the
    join's condition: orders.order_id in "orders.order_id = order_items.order_id"); [] when nothing
    repeats or a key cannot be read (then the fan-out check decides)."""
    pairs = fan_out_joins(metric.name, tables, registry)
    keys: list[str] = []
    for one, many in pairs:
        join = next((j for j in registry.list_joins() if {j.from_table, j.to_table} == {one, many}), None)
        columns = re.findall(rf"\b{re.escape(one)}\.([A-Za-z_][A-Za-z0-9_]*)", join.on) if join else []
        if not columns:
            return []
        keys += [_apply_aliases(f"{one}.{column}", aliases) for column in dict.fromkeys(columns)]
    return list(dict.fromkeys(keys))


def _build_join_plan(
    base_table: str,
    required_tables: set[str],
    registry: SemanticRegistry,
) -> list[SemanticJoin]:
    graph = _build_join_graph(registry)
    join_plan: list[SemanticJoin] = []
    seen_joins: set[tuple[str, str, str, str]] = set()

    for target_table in sorted(required_tables - {base_table}):
        path = _find_join_path(base_table, target_table, graph)
        if path is None:
            raise ValueError(
                "Could not resolve join path for all required tables. "
                f"Missing tables: {sorted(required_tables - {base_table})}"
            )

        for join in path:
            join_id = _join_key(join)
            if join_id in seen_joins:
                continue
            seen_joins.add(join_id)
            join_plan.append(join)

    return join_plan


def build_sql_from_intent(
    intent: SemanticIntent,
    registry: SemanticRegistry,
    *,
    table_ref: TableRefResolver | None = None,
) -> str:
    metric = registry.get_metric(intent.metric)
    required_tables: set[str] = set()
    aliases: dict[str, str] = {}

    _add_required_tables(required_tables, aliases, metric.required_tables)
    metric_expression = _apply_aliases(_build_metric_expression(metric), aliases)

    select_parts: list[str] = []
    group_parts: list[str] = []
    where_parts: list[str] = []
    dimension_aliases: list[str] = []

    if metric.base_filter:
        where_parts.append(_apply_aliases(metric.base_filter, aliases))

    for dimension_name in intent.dimensions:
        dimension = registry.get_dimension(dimension_name)
        _add_required_tables(required_tables, aliases, dimension.required_tables)
        dimension_expr = _apply_aliases(dimension.sql_expression, aliases)
        if dimension.separator:
            # One row per item. GROUP BY repeats the expression: the alias usually equals the
            # column name, and GROUP BY <name> binds to the input column (the raw list).
            dimension_expr = f"unnest({items_expression(dimension_expr, dimension.separator)})"
        select_parts.append(f"{dimension_expr} AS {dimension.name}")
        group_parts.append(dimension_expr)
        dimension_aliases.append(dimension.name)

    if intent.time_dimension:
        time_dimension = registry.get_time_dimension(intent.time_dimension)
        # The time dimension's table must be joined in even when the metric does not need it
        # (a generated schema's revenue lives in payments, its order date in orders).
        if time_dimension.table not in required_tables:  # keep an alias the metric declared
            _add_required_tables(required_tables, aliases, [time_dimension.table])
        granularity = intent.time_granularity or time_dimension.default_granularity
        time_expr = _apply_aliases(time_dimension.sql_expression, aliases)
        bucket_expr = _time_bucket_expression(time_expr, granularity)
        select_parts.append(f"{bucket_expr} AS time_bucket")
        group_parts.append(bucket_expr)

    for filter_condition in intent.filters:
        dimension = registry.get_dimension(filter_condition.dimension)
        _add_required_tables(required_tables, aliases, dimension.required_tables)
        filter_expr = _apply_aliases(dimension.sql_expression, aliases)
        where_parts.append(_build_filter_clause(filter_condition, filter_expr, dimension.separator))

    having_parts: list[str] = []
    for threshold in intent.metric_filters:
        # Per group, after aggregation: "albums with more than 5 tracks" (Chinook).
        threshold_metric = registry.get_metric(threshold.metric)
        _add_required_tables(required_tables, aliases, threshold_metric.required_tables)
        threshold_expr = _apply_aliases(_build_metric_expression(threshold_metric), aliases)
        operator = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[threshold.operator]
        having_parts.append(f"{threshold_expr} {operator} {_to_sql_literal(threshold.value)}")

    if intent.start_date or intent.end_date:
        time_dimension_name = intent.time_dimension or registry.default_time_dimension(intent.metric)
        if time_dimension_name is None:
            raise ValueError("No time dimension available for date filter")
        time_dimension = registry.get_time_dimension(time_dimension_name)
        if time_dimension.table not in required_tables:  # keep an alias the metric declared
            _add_required_tables(required_tables, aliases, [time_dimension.table])
        time_expr = _apply_aliases(time_dimension.sql_expression, aliases)
        if intent.start_date:
            where_parts.append(f"{time_expr} >= {_to_sql_literal(intent.start_date)}")
        if intent.end_date:
            # end_date is an inclusive calendar day; the column may be a timestamp.
            where_parts.append(f"{time_expr} < {_next_day(intent.end_date)}")

    base_table = metric.required_tables[0].split()[0]
    from_clause = _format_table_with_alias(base_table, aliases.get(base_table, base_table), table_ref)

    connected_tables = {base_table}
    join_clauses: list[str] = []
    for join in _build_join_plan(base_table, required_tables, registry):
        join_table: str | None = None
        if join.from_table in connected_tables and join.to_table not in connected_tables:
            join_table = join.to_table
        elif join.to_table in connected_tables and join.from_table not in connected_tables:
            join_table = join.from_table
        elif join.from_table in connected_tables and join.to_table in connected_tables:
            continue
        else:
            raise ValueError(
                "Resolved join plan contains a disconnected edge. "
                f"Edge: {join.from_table}->{join.to_table}"
            )

        join_alias = aliases.get(join_table, join_table)
        on_sql = _apply_aliases(join.on, aliases)
        join_target = _format_table_with_alias(join_table, join_alias, table_ref)
        join_clauses.append(f"{join.join_type} JOIN {join_target} ON {on_sql}")
        connected_tables.add(join_table)

    sql_parts: list[str] = []
    grain_keys = _average_grain_keys(metric, required_tables, registry, aliases) if metric.aggregation == "AVG" else []
    if grain_keys:
        # An average across a one-to-many join: each "one" row (an order) once per group, not once per
        # "many" row (item). Weighting by items gave 4 days for orders of 2 days (3 items) and 10 days
        # (1 item); per order it is 6 (second review).
        if having_parts:
            raise ValueError("A threshold on an average across a one-to-many join is not supported")
        value = _apply_aliases(_strip_aggregate(metric.sql_expression, "AVG"), aliases)
        # One row per (key, group), its value averaged over that key's own rows (an order with two
        # reviews counts once, at their mean). GROUP BY, not SELECT DISTINCT: Jammi's pushdown to
        # Postgres breaks a DISTINCT derived table whose output names equal its columns (J38).
        inner = ([f"{key} AS _grain_{i}" for i, key in enumerate(grain_keys)] + select_parts
                 + [f"AVG(CAST(({value}) AS DOUBLE)) AS _value"])
        outputs = dimension_aliases + (["time_bucket"] if intent.time_dimension else [])
        sql_parts.append("SELECT " + ", ".join(outputs + ["AVG(_value) AS metric_value"]))
        sql_parts.append("FROM (")
        sql_parts.append("SELECT " + ", ".join(inner))
        sql_parts.append(f"FROM {from_clause}")
        sql_parts.extend(join_clauses)
        if where_parts:
            sql_parts.append("WHERE " + " AND ".join(where_parts))
        sql_parts.append("GROUP BY " + ", ".join(grain_keys + group_parts))
        sql_parts.append(") AS per_row")
        if outputs:
            sql_parts.append("GROUP BY " + ", ".join(outputs))
        sql_parts.append(_order_by_clause(intent.order_by, has_time=bool(intent.time_dimension),
                                          tie_breakers=dimension_aliases))
        sql_parts.append(f"LIMIT {intent.limit}")
        return "\n".join(sql_parts)

    select_clause = ", ".join(select_parts + [f"{metric_expression} AS metric_value"])
    sql_parts.append(f"SELECT {select_clause}")
    sql_parts.append(f"FROM {from_clause}")
    sql_parts.extend(join_clauses)

    if where_parts:
        sql_parts.append("WHERE " + " AND ".join(where_parts))
    if group_parts:
        sql_parts.append("GROUP BY " + ", ".join(group_parts))
    if having_parts:
        sql_parts.append("HAVING " + " AND ".join(having_parts))
    sql_parts.append(
        _order_by_clause(
            intent.order_by,
            has_time=bool(intent.time_dimension),
            tie_breakers=dimension_aliases,
        )
    )
    sql_parts.append(f"LIMIT {intent.limit}")

    return "\n".join(sql_parts)
