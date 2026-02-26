from __future__ import annotations

import re

from app.models.semantic_intent import FilterCondition, SemanticIntent

from .loader import SemanticRegistry


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


def _build_metric_expression(metric) -> str:
    if metric.aggregation in {"SUM", "AVG", "COUNT"}:
        return f"{metric.aggregation}({metric.sql_expression})"
    if metric.aggregation == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {metric.sql_expression})"
    if metric.aggregation == "RATIO":
        return f"({metric.numerator_sql})::DOUBLE / NULLIF(({metric.denominator_sql}), 0)"
    raise ValueError(f"Unsupported aggregation: {metric.aggregation}")


def _to_sql_literal(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _build_filter_clause(filter_condition: FilterCondition, expression: str) -> str:
    operator = filter_condition.operator
    value = filter_condition.value

    if operator == "eq":
        return f"{expression} = {_to_sql_literal(value)}"
    if operator == "ne":
        return f"{expression} <> {_to_sql_literal(value)}"
    if operator == "gt":
        return f"{expression} > {_to_sql_literal(value)}"
    if operator == "gte":
        return f"{expression} >= {_to_sql_literal(value)}"
    if operator == "lt":
        return f"{expression} < {_to_sql_literal(value)}"
    if operator == "lte":
        return f"{expression} <= {_to_sql_literal(value)}"
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
        return f"{expression} BETWEEN {_to_sql_literal(value[0])} AND {_to_sql_literal(value[1])}"
    raise ValueError(f"Unsupported filter operator: {operator}")


def _time_bucket_expression(time_expr: str, granularity: str) -> str:
    return f"DATE_TRUNC('{granularity}', {time_expr})"


def build_sql_from_intent(intent: SemanticIntent, registry: SemanticRegistry) -> str:
    metric = registry.get_metric(intent.metric)
    required_tables: set[str] = set()
    aliases: dict[str, str] = {}

    _add_required_tables(required_tables, aliases, metric.required_tables)
    metric_expression = _apply_aliases(_build_metric_expression(metric), aliases)

    select_parts: list[str] = []
    group_parts: list[str] = []
    where_parts: list[str] = []

    if metric.base_filter:
        where_parts.append(_apply_aliases(metric.base_filter, aliases))

    for dimension_name in intent.dimensions:
        dimension = registry.get_dimension(dimension_name)
        _add_required_tables(required_tables, aliases, dimension.required_tables)
        dimension_expr = _apply_aliases(dimension.sql_expression, aliases)
        select_parts.append(f"{dimension_expr} AS {dimension.name}")
        group_parts.append(dimension_expr)

    if intent.time_dimension:
        time_dimension = registry.get_time_dimension(intent.time_dimension)
        granularity = intent.time_granularity or time_dimension.default_granularity
        time_expr = _apply_aliases(time_dimension.sql_expression, aliases)
        bucket_expr = _time_bucket_expression(time_expr, granularity)
        select_parts.append(f"{bucket_expr} AS time_bucket")
        group_parts.append(bucket_expr)

    for filter_condition in intent.filters:
        dimension = registry.get_dimension(filter_condition.dimension)
        _add_required_tables(required_tables, aliases, dimension.required_tables)
        filter_expr = _apply_aliases(dimension.sql_expression, aliases)
        where_parts.append(_build_filter_clause(filter_condition, filter_expr))

    if intent.start_date or intent.end_date:
        time_dimension_name = intent.time_dimension or "order_date"
        time_dimension = registry.get_time_dimension(time_dimension_name)
        time_expr = _apply_aliases(time_dimension.sql_expression, aliases)
        if intent.start_date:
            where_parts.append(f"{time_expr} >= {_to_sql_literal(intent.start_date)}")
        if intent.end_date:
            where_parts.append(f"{time_expr} <= {_to_sql_literal(intent.end_date)}")

    base_table = metric.required_tables[0].split()[0]
    from_clause = f"{base_table} {aliases.get(base_table, base_table)}"

    connected_tables = {base_table}
    pending_tables = set(required_tables) - connected_tables
    join_clauses: list[str] = []

    while pending_tables:
        progress = False
        for join in registry.list_joins():
            left = join.from_table
            right = join.to_table
            if left in connected_tables and right in pending_tables:
                right_alias = aliases.get(right, right)
                on_sql = _apply_aliases(join.on, aliases)
                join_clauses.append(f"{join.join_type} JOIN {right} {right_alias} ON {on_sql}")
                connected_tables.add(right)
                pending_tables.remove(right)
                progress = True
                break
            if right in connected_tables and left in pending_tables:
                left_alias = aliases.get(left, left)
                on_sql = _apply_aliases(join.on, aliases)
                join_clauses.append(f"{join.join_type} JOIN {left} {left_alias} ON {on_sql}")
                connected_tables.add(left)
                pending_tables.remove(left)
                progress = True
                break
        if not progress:
            raise ValueError(
                "Could not resolve join path for all required tables. "
                f"Missing tables: {sorted(pending_tables)}"
            )

    sql_parts: list[str] = []
    select_clause = ", ".join(select_parts + [f"{metric_expression} AS metric_value"])
    sql_parts.append(f"SELECT {select_clause}")
    sql_parts.append(f"FROM {from_clause}")
    sql_parts.extend(join_clauses)

    if where_parts:
        sql_parts.append("WHERE " + " AND ".join(where_parts))
    if group_parts:
        sql_parts.append("GROUP BY " + ", ".join(group_parts))
    if intent.order_by == "metric_asc":
        sql_parts.append("ORDER BY metric_value ASC")
    else:
        sql_parts.append("ORDER BY metric_value DESC")
    sql_parts.append(f"LIMIT {intent.limit}")

    return "\n".join(sql_parts)
