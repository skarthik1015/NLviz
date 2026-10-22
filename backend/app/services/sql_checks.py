"""The semantic protections, for SQL the model writes (the SQL-primary path, docs/ablation-results.md
"Decision gate"). The intent checks (app.agent.ambiguity) look at an intent; these look at the SQL,
by its syntax tree, against the same semantic layer:

- fan_out: a SUM, AVG or COUNT of a column of one table, in a query that also joins a table with
  several rows per row of it (orders joined to their lines), repeats each value once per line;
  confirmed on the data when it can be, since conditions can keep one row per key;
- distinct_amounts: SUM or AVG of DISTINCT values is refused: two orders can have equal amounts,
  and removing duplicate amounts does not remove duplicate rows;
- policy: an aggregate over a metric's column must keep the metric's agreed condition (`base_filter`:
  revenue without canceled orders), unless the question asks otherwise and the model says so
  (`definitions_overridden`). Applies to any expression over that column, not only the metric's own;
- definitions (`ask_definitions`, the app's default): the data model's conditions are inferred, not
  agreed. One is applied without asking only when the question states it, the user chose it in this
  conversation, the owner confirmed it for the connection, or the data shows it excludes no rows of
  this query. Otherwise, and when matching definitions disagree, the user is asked before anything
  runs, and the answer is enforced like any other choice;
- mixed_units: a SUM or AVG of a metric that has a unit column (currency) must not put several
  units in one figure; checked on the data, per group of the query, as the intent check does;
- zero_placeholder: an AVG of a column whose zeros may mean "not recorded" asks whether to leave
  them out, unless the SQL already says;
- values: `column = 'text'` (or IN) on a column of the layer, checked against the connection's value
  index: the stored spelling goes back to the model ('Asia' -> 'ASIA', a synonym -> its code), and a
  value only weakly close to real ones is asked about ("Rio": RJ, RN or RS?), as the compiler path
  does. A column the index does not cover (a city, a name) is checked on the data: a value no row
  has, with values containing it ("rio de janeiro", "rio grande"), is asked about the same way. A
  value with no candidate is left alone: an empty answer can be right.

A user's answer to a choice is a constraint the next SQL is checked against (`decisions`), not a
reason to skip the check.

Columns are traced through CTEs and subqueries to the table they come from, with the conditions of
every scope on the way; a column that cannot be traced (through a UNION, a window, an ambiguous
name) is sent back to the model, since nothing about it can be shown. An aggregate over a column a
subquery already aggregated is checked where that aggregate is.

`check_sql` returns a Verdict: a repair (the reason goes back to the model), a clarification (a
choice for the user; each choice carries the instruction given to the model and the constraint it
sets), or notes for the answer.

Reviewed 2026-10-18 to 2026-10-21 (CTE lineage, SUM(DISTINCT), literal case, choices
enforced, NULLIF, metric expressions).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from app.semantic.sql_builder import fanned_tables

logger = logging.getLogger(__name__)

Run = Callable[[str], list[dict]]  # logical SQL -> rows
Physical = tuple[str, str]  # (table, column)

PROBE_ROWS = 5000
MAX_UNIT_CHOICES = 6
MAX_DEPTH = 8
BREAK_DOWN = "by"
ZERO_EXCLUDE = "exclude_zeros"
ZERO_INCLUDE = "include_zeros"
# Casts that only change the type, not the value (the pushdown guard adds AVG(CAST(x AS DOUBLE))).
# DECIMAL(18, 0) rounds, so it stays part of the expression.
_FLOAT_TYPES = {exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT}


@dataclass
class Verdict:
    repair: str | None = None  # why the SQL gives a wrong answer, for the model
    reason: str | None = None  # the check that found it
    clarification: dict | None = None
    notes: list[str] = field(default_factory=list)
    measures: list[dict] = field(default_factory=list)  # aggregates matched to metrics, for the panel
    policies: list[dict] = field(default_factory=list)  # definitions applied or overridden


@dataclass
class _Lineage:
    columns: set[Physical] = field(default_factory=set)
    scopes: list = field(default_factory=list)  # the _Scopes whose conditions apply to these rows
    aggregated: bool = False  # the value was aggregated in a subquery (checked there)
    unchanged: bool = True  # the column's values pass through as they are (not `x * 100`)
    via: set[str] = field(default_factory=set)  # aliases of derived sources in the first scope


@dataclass
class _Aggregate:
    node: exp.AggFunc
    kind: str  # SUM, AVG, COUNT
    distinct: bool
    argument: exp.Expression
    columns: frozenset[Physical]
    scopes: list  # this scope first, then the scopes the columns were traced through
    traced: bool  # False: some column could not be traced
    aggregated: bool  # over values a subquery aggregated
    via: frozenset[str] = frozenset()  # derived sources (aliases in this scope) the values came through

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(t for t, _ in self.columns)


# --------------------------------------------------------------------------------- definitions
def _aliases(required_tables: list[str]) -> dict[str, str]:
    """Alias (or name) -> table, from a definition's required tables ("order_payments op")."""
    out: dict[str, str] = {}
    for entry in required_tables or ():
        parts = entry.split()
        out[parts[0]] = parts[0]
        if len(parts) > 1:
            out[parts[-1]] = parts[0]
    return out


def _definition_resolver(aliases: dict[str, str]) -> Callable[[exp.Column], Physical | None]:
    only = next(iter(set(aliases.values()))) if len(set(aliases.values())) == 1 else None

    def resolve(column: exp.Column) -> Physical | None:
        table = aliases.get(column.table) if column.table else only
        return (table, column.name) if table else None

    return resolve


def _parse(sql: str | None) -> exp.Expression | None:
    if not sql:
        return None
    try:
        return sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return None


def _definition_columns(sql: str | None, aliases: dict[str, str]) -> frozenset[Physical]:
    tree = _parse(sql)
    if tree is None:
        return frozenset()
    resolve = _definition_resolver(aliases)
    return frozenset(p for p in (resolve(c) for c in tree.find_all(exp.Column)) if p)


def _canonical(node: exp.Expression, resolve: Callable[[exp.Column], Physical | None]) -> str:
    """The expression's SQL with every column written table.column in lower case, literals as they
    are ('CANCELED' is not 'canceled'), casts to a floating type removed, and the operands of + and
    * in a fixed order: the equivalences two definitions may differ by."""
    def rewrite(part: exp.Expression) -> exp.Expression:
        if isinstance(part, exp.Column) and not isinstance(part.this, exp.Star):
            found = resolve(part)
            table, name = found if found else (part.table, part.name)
            return exp.column(name.lower(), table=table.lower() if table else None)
        if isinstance(part, exp.Cast) and part.to.this in _FLOAT_TYPES and not part.to.expressions:
            return part.this
        return part

    tree = node.copy().transform(rewrite)
    for operation in list(tree.find_all(exp.Add, exp.Mul)):
        left, right = sorted((operation.left, operation.right), key=lambda e: e.sql())
        operation.set("this", left)
        operation.set("expression", right)
    return tree.sql(dialect="postgres")


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return [*_conjuncts(node.left), *_conjuncts(node.right)]
    return [node]


# ------------------------------------------------------------------------------------- scopes
class _Scope:
    """One SELECT: its real tables and derived sources by alias, how a column resolves to a real
    table's column (through CTEs and subqueries), and its conditions."""

    def __init__(self, scope, columns_by_table: dict[str, set[str]]):
        self.scope = scope
        self.select = scope.expression
        self.tables: dict[str, str] = {}
        self.derived: dict[str, object] = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                self.tables[alias] = source.name
            else:
                self.derived[alias] = source
        self._columns = columns_by_table

    def table_of(self, column: exp.Column) -> str | None:
        if column.table:
            return self.tables.get(column.table)
        owners = {t for t in self.tables.values() if column.name in self._columns.get(t, ())}
        if len(owners) == 1 and not any(self._outputs(s, column.name) for s in self.derived.values()):
            return next(iter(owners))
        return None

    # -- lineage
    @staticmethod
    def _outputs(source, name: str) -> bool:
        select = getattr(source, "expression", None)
        if not isinstance(select, exp.Select):
            return False
        return any(e.alias_or_name == name or isinstance(e, exp.Star) or
                   (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions)

    def _derived_source(self, column: exp.Column):
        if column.table:
            return self.derived.get(column.table)
        if any(column.name in self._columns.get(t, ()) for t in self.tables.values()):
            return None
        candidates = [s for s in self.derived.values() if self._outputs(s, column.name)]
        return candidates[0] if len(candidates) == 1 else None

    def trace(self, column: exp.Column, depth: int = 0) -> _Lineage | None:
        """Where the column's values come from; None when that cannot be shown."""
        table = self.table_of(column)
        if table:
            return _Lineage({(table, column.name)}, [self])
        source = self._derived_source(column)
        if source is None or depth > MAX_DEPTH or not isinstance(source.expression, exp.Select):
            return None  # an unknown or ambiguous name, a UNION, too deep
        child = _Scope(source, self._columns)
        output = next((e for e in child.select.expressions
                       if not isinstance(e, exp.Star) and e.alias_or_name == column.name
                       and not (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))), None)
        if output is None:
            # SELECT * / t.*: the column passes through from the child's sources.
            if not any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
                       for e in child.select.expressions):
                return None
            passed = child.trace(exp.column(column.name), depth + 1)
            return _Lineage(passed.columns, [self, *passed.scopes], passed.aggregated, passed.unchanged,
                            {self._alias_of(source)}) if passed else None
        value = output.this if isinstance(output, exp.Alias) else output
        if value.find(exp.Window):
            return None
        if value.find(exp.AggFunc):
            return _Lineage(set(), [self, child], aggregated=True, unchanged=False, via={self._alias_of(source)})
        merged = _Lineage(set(), [self, child], unchanged=isinstance(value, exp.Column), via={self._alias_of(source)})
        for part in value.find_all(exp.Column):
            found = child.trace(part, depth + 1)
            if found is None or found.aggregated:
                return None
            merged.columns |= found.columns
            merged.unchanged &= found.unchanged
            merged.scopes += [s for s in found.scopes if s not in merged.scopes]
        return merged

    def _alias_of(self, source) -> str:
        return next(alias for alias, s in self.derived.items() if s is source)

    def resolve(self, column: exp.Column) -> Physical | None:
        """A column as the real table's column it passes through unchanged, else None."""
        table = self.table_of(column)
        if table:
            return table, column.name
        found = self.trace(column)
        if found and found.unchanged and not found.aggregated and len(found.columns) == 1:
            return next(iter(found.columns))
        return None  # transformed on the way (x * 100) or not traceable: not the column itself

    # -- clauses
    def where(self) -> list[exp.Expression]:
        """WHERE conditions and those in the ON of inner joins: both remove rows."""
        where = self.select.args.get("where")
        found = _conjuncts(where.this if where is not None else None)
        for join in self.select.args.get("joins") or ():
            if (join.args.get("side") or "").upper() in ("", "INNER") and join.args.get("on") is not None:
                found += _conjuncts(join.args["on"])
        return found

    def group_keys(self) -> list[exp.Expression]:
        """GROUP BY expressions, with ordinals (GROUP BY 1) and output aliases resolved."""
        group = self.select.args.get("group")
        if group is None:
            return []
        outputs = self.select.expressions
        named = {e.alias: e.this for e in outputs if isinstance(e, exp.Alias)}
        keys = []
        for key in group.expressions:
            if isinstance(key, exp.Literal) and key.is_int and 0 < int(key.this) <= len(outputs):
                chosen = outputs[int(key.this) - 1]
                keys.append(chosen.this if isinstance(chosen, exp.Alias) else chosen)
            elif isinstance(key, exp.Column) and not key.table and key.name in named and self.resolve(key) is None:
                keys.append(named[key.name])
            else:
                keys.append(key)
        return keys

    def aggregates(self) -> list[_Aggregate]:
        found = []
        for node in self.select.find_all(exp.Sum, exp.Avg, exp.Count):
            if node.find_ancestor(exp.Select) is not self.select:
                continue
            argument = node.this
            distinct = isinstance(argument, exp.Distinct)
            if distinct:
                argument = argument.expressions[0] if argument.expressions else None
            if argument is None or isinstance(argument, exp.Star):
                continue
            parts = list(argument.find_all(exp.Column))
            if not parts:
                continue  # COUNT(1)
            columns: set[Physical] = set()
            scopes = [self]
            traced, aggregated = True, False
            via: set[str] = set()
            for part in parts:
                lineage = self.trace(part)
                if lineage is None:
                    traced = False
                    break
                aggregated |= lineage.aggregated
                via |= lineage.via
                columns |= lineage.columns
                scopes += [s for s in lineage.scopes if s not in scopes]
            if aggregated and columns:
                traced = False  # a subquery's aggregate mixed with raw values: no single grain
            found.append(_Aggregate(node, type(node).__name__.upper(), distinct, argument, frozenset(columns),
                                    scopes, traced, aggregated, frozenset(via)))
        return found


def _conditions(aggregate: _Aggregate) -> list[tuple[exp.Expression, "_Scope"]]:
    return [(condition, scope) for scope in aggregate.scopes for condition in scope.where()]


def _argument_key(aggregate: _Aggregate) -> str:
    scope = aggregate.scopes[0]
    return _canonical(aggregate.argument, scope.resolve)


# ------------------------------------------------------------------------------------------ checks
def _repeats_rows(scope: _Scope, base: str, run: Run | None) -> bool:
    """Whether, under the query's own joins and conditions, some row of `base` comes out more than
    once. The join graph says a join *can* repeat rows; conditions often keep one row per key
    (today's salary and today's department of each employee), and then nothing repeats. Run on the
    data, grouped by every column of `base`; True when it cannot be shown otherwise."""
    alias = next((a for a, t in scope.tables.items() if t == base), None)
    columns = sorted(scope._columns.get(base, ()))
    if run is None or alias is None or not columns:
        return True
    keys = [exp.column(c, table=alias) for c in columns]
    probe = _probe_select(scope, keys, [exp.Count(this=exp.Star()).as_("copies")])
    probe.set("group", exp.Group(expressions=[k.copy() for k in keys]))
    probe = probe.having("COUNT(*) > 1").limit(1)
    try:
        return bool(run(probe.sql(dialect="postgres")))
    except Exception as exc:  # noqa: BLE001 - unproven is not safe
        logger.warning("Fan-out probe failed: %s", exc)
        return True


def _rejoined(aggregate: _Aggregate, scope: _Scope, run: Run | None) -> str | None:
    """An aggregate over values a subquery already aggregated, in a query that joins that subquery to
    other rows: each of its rows can come out several times (per-order totals joined back to order
    lines). Shown on the data: grouped by every output of the subquery, no row may repeat. When that
    cannot be shown, the query goes back to the model."""
    if len(scope.tables) + len(scope.derived) <= 1:
        return None
    for alias in aggregate.via:
        source = scope.derived.get(alias)
        select = getattr(source, "expression", None)
        outputs = [e.alias_or_name for e in getattr(select, "expressions", ())]
        if not isinstance(select, exp.Select) or not outputs or any(
                isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
                for e in select.expressions):
            return (f"{aggregate.node.sql()} adds up values {alias} already aggregated, joined to other rows; name "
                    f"{alias}'s columns so the checks can show the join does not repeat them.")
        keys = [exp.column(name, table=alias) for name in outputs]
        probe = _probe_select(scope, keys, [exp.Count(this=exp.Star()).as_("copies")])
        probe.set("group", exp.Group(expressions=[k.copy() for k in keys]))
        repeated = True
        if run is not None:
            try:
                repeated = bool(run(probe.having("COUNT(*) > 1").limit(1).sql(dialect="postgres")))
            except Exception as exc:  # noqa: BLE001 - unproven is not safe
                logger.warning("Rejoin probe failed: %s", exc)
        if repeated:
            return (f"{aggregate.node.sql()} adds up values {alias} already aggregated, but the query joins {alias} "
                    f"to other rows that repeat each {alias} row, so each value is counted several times. Join "
                    f"tables at the same grain (aggregate the other side per the same key first) or filter with "
                    "EXISTS / IN instead of a join.")
    return None


def _declared_keys(registry) -> list[frozenset[frozenset[Physical]]]:
    """Each data-model join's key, as its set of column pairs: {orders.order_id = order_items.order_id},
    or both pairs of a composite key (account_id and order_id). A join is declared only when the SQL
    links every pair of one key: one part of a composite key repeats rows."""
    keys = []
    for join in registry.list_joins():
        resolve = _definition_resolver({join.from_table: join.from_table, join.to_table: join.to_table})
        pairs = set()
        for condition in _conjuncts(_parse(join.on)):
            if isinstance(condition, exp.EQ) and isinstance(condition.this, exp.Column) \
                    and isinstance(condition.expression, exp.Column):
                pair = (resolve(condition.this), resolve(condition.expression))
                if all(pair):
                    pairs.add(frozenset(pair))
        if pairs:
            keys.append(frozenset(pairs))
    return keys


def _singleton(source: exp.Expression) -> bool:
    """A derived table that returns one row: SELECT without FROM, or only aggregates and no GROUP BY
    ((SELECT 1 AS k), (SELECT SUM(x) AS total FROM t))."""
    select = source.this if isinstance(source, exp.Subquery) else None
    if not isinstance(select, exp.Select) or select.args.get("group") is not None:
        return False
    if select.args.get("from_") is None:
        return True
    return all((e.this if isinstance(e, exp.Alias) else e).find(exp.AggFunc) is not None
               and not (e.this if isinstance(e, exp.Alias) else e).find(exp.Window) for e in select.expressions)


def _join_shape(scope: _Scope, registry) -> tuple[str | None, bool]:
    """(a problem, whether some join uses keys the data model does not declare). A join is linked
    only by a top-level equality between a column of the joined table and a column of another one:
    `ON c.customer_id = o.customer_id OR 1 = 1` and `ON other.order_id = other.order_id` link nothing
    and multiply every row. A one-row derived table needs no link. A link on undeclared columns is
    not a known cardinality: the caller shows on the data that it does not repeat rows."""
    where = _conjuncts((scope.select.args.get("where") or exp.Where()).this)
    declared = _declared_keys(registry)
    unproven = False
    for join in scope.select.args.get("joins") or ():
        joined = join.this.alias_or_name if isinstance(join.this, (exp.Table, exp.Subquery)) else None
        if joined is None:
            continue
        if _singleton(join.this):
            continue
        if join.args.get("using"):
            unproven = True
            continue
        links = []
        for condition in _conjuncts(join.args.get("on")) + where:
            if isinstance(condition, exp.EQ) and isinstance(condition.this, exp.Column) \
                    and isinstance(condition.expression, exp.Column):
                sides = {condition.this.table, condition.expression.table}
                if joined in sides and sides - {joined, ""}:
                    links.append(condition)
        if not links:
            return (f"the join to {joined} does not link it to the other tables: its ON condition needs an "
                    f"equality between a column of {joined} and a column of another table, joined by AND (an OR, "
                    "or a comparison of the table with itself, links nothing), so every row is repeated."), unproven
        linked_pairs = {frozenset((scope.resolve(c.this), scope.resolve(c.expression))) for c in links}
        if not any(key <= linked_pairs for key in declared):
            unproven = True
    return None, unproven


def _duplicated(aggregate: _Aggregate, tables: set[str]) -> str | None:
    """A table the check depends on, joined more than once in the aggregate's scopes: which copy a
    condition applies to cannot be shown (a filter on another order is not a filter on this one)."""
    for scope in aggregate.scopes:
        seen: dict[str, int] = {}
        for table in scope.tables.values():
            seen[table] = seen.get(table, 0) + 1
        twice = sorted(t for t, n in seen.items() if n > 1 and t in tables)
        if twice:
            return (f"{twice[0]} is joined more than once, so the checks cannot tell which copy a condition applies "
                    f"to. Join {twice[0]} once, or compute the other copy's part in a subquery.")
    return None


def _fan_out(aggregate: _Aggregate, registry, run: Run | None = None, verdict: Verdict | None = None,
             unproven: bool = False) -> str | None:
    if (aggregate.kind == "COUNT" and aggregate.distinct) or aggregate.aggregated or len(aggregate.tables) != 1:
        return None
    base = next(iter(aggregate.tables))
    tables = {t for scope in aggregate.scopes for t in scope.tables.values()}
    fanned = fanned_tables(base, tables, registry)
    if not fanned and unproven:
        # The joins are not the data model's: their cardinality is unknown, so ask the data.
        if len(aggregate.scopes) == 1 and not _repeats_rows(aggregate.scopes[0], base, run):
            return None
        return (f"{aggregate.node.sql()} aggregates a column of {base}, and the query joins on columns that are not "
                f"the data model's join keys, so it cannot be shown that each {base} row is counted once. Join on "
                "the declared keys, or aggregate the other side per key first.")
    if not fanned:
        return None
    one, many = fanned[0]
    # The data can only be asked in one scope; across subqueries the join graph decides.
    if len(aggregate.scopes) == 1 and not _repeats_rows(aggregate.scopes[0], base, run):
        if verdict is not None:
            verdict.notes.append(f"{many} can have several rows per {one} row; checked on the data that this "
                                 f"query's conditions keep one, so no {base} value is counted twice.")
        return None
    return (f"{aggregate.node.sql()} aggregates a column of {base}, but the query also joins {many}, which has "
            f"several rows per {one} row, so each {base} value is counted once per {many} row. Aggregate "
            f"{many} in a subquery grouped by the {one} key first, filter with EXISTS / IN instead of a join, "
            f"or use a measure recorded on {many}.")


def _metric_kind(aggregate: _Aggregate) -> str:
    return "COUNT_DISTINCT" if aggregate.kind == "COUNT" and aggregate.distinct else aggregate.kind


def _metric_key(metric) -> str | None:
    tree = _parse(metric.sql_expression)
    return _canonical(tree, _definition_resolver(_aliases(metric.required_tables))) if tree is not None else None


def _matching_metrics(aggregate: _Aggregate, registry) -> tuple[list, list]:
    """(metrics this aggregate computes: same aggregation and the same expression, up to the
    equivalences in _canonical; metrics over the same columns with the same aggregation, whose
    conditions apply to it too: SUM(payment_value * 100) still counts only valid orders)."""
    exact, related = [], []
    if not aggregate.columns:
        return exact, related
    key = _argument_key(aggregate)
    for metric in registry.schema.metrics:
        if metric.aggregation == "RATIO" or metric.aggregation != _metric_kind(aggregate):
            continue
        if _definition_columns(metric.sql_expression, _aliases(metric.required_tables)) != aggregate.columns:
            continue
        (exact if _metric_key(metric) == key else related).append(metric)
    return exact, related


def _implied_not_null(condition: exp.Expression, resolve, aggregate: _Aggregate) -> bool:
    """`col IS NOT NULL` on a column the aggregate reads: SQL aggregates skip nulls already."""
    if not (isinstance(condition, exp.Not) and isinstance(condition.this, exp.Is)
            and isinstance(condition.this.expression, exp.Null) and isinstance(condition.this.this, exp.Column)):
        return False
    return resolve(condition.this.this) in aggregate.columns


def _definition_key(aggregate: _Aggregate) -> str:
    """What a definition decision is about: the measured columns ("order_payments.payment_value")."""
    return ",".join(sorted(f"{t}.{c}" for t, c in aggregate.columns))


def _describe_condition(condition: str | None) -> str:
    """A condition in words, for a choice: "Leave out canceled, unavailable (order status)"."""
    if condition is None:
        return "Count every row (no condition)"
    tree = _parse(condition)
    negated = isinstance(tree, exp.Not)
    inner = tree.this if negated else tree
    if isinstance(inner, exp.In) and isinstance(inner.this, exp.Column):
        values = ", ".join(str(v.this) for v in inner.expressions if isinstance(v, exp.Literal))
        column = inner.this.name.replace("_", " ")
        return f"{'Leave out' if negated else 'Only'} {values} ({column})"
    if isinstance(tree, (exp.EQ, exp.NEQ)) and isinstance(tree.this, exp.Column) and isinstance(tree.expression, exp.Literal):
        column = tree.this.name.replace("_", " ")
        return f"{'Only' if isinstance(tree, exp.EQ) else 'Leave out'} {tree.expression.this} ({column})"
    return f"Only rows where {condition}"


def _literals(node: exp.Expression) -> list[str]:
    return [str(literal.this) for literal in node.find_all(exp.Literal) if literal.is_string]


def _restrictions(aggregate: _Aggregate, columns: set[Physical]) -> list[tuple[exp.Expression, "_Scope"]]:
    """Every condition on one of the columns where it filters the answer's rows (WHERE and inner-join
    ON, in each scope the values pass through), with any operator: =, <>, IN, IS NOT NULL, LIKE, ...
    Join keys (a column of one table = a column of another) are not restrictions; `status = status`
    is one (it drops NULL statuses)."""
    found = []
    for condition, scope in _conditions(aggregate):
        if isinstance(condition, exp.EQ) and isinstance(condition.this, exp.Column) \
                and isinstance(condition.expression, exp.Column):
            left, right = scope.resolve(condition.this), scope.resolve(condition.expression)
            if left and right and left[0] != right[0]:
                continue
        if any(scope.resolve(column) in columns for column in condition.find_all(exp.Column)):
            found.append((condition, scope))
    return found


def _excludes_rows(aggregate: _Aggregate, metric, condition: str, run: Run | None) -> bool:
    """Whether the condition leaves out any row this query would otherwise count: the query's own
    tables and conditions without the condition, with NOT condition. True when that cannot be shown:
    no data, a table the condition needs is not in the query, more than one scope, or the condition
    sits in a join (removing it from an ON changes the join, so the comparison would not be sound)."""
    if run is None or len(aggregate.scopes) != 1:
        return True
    scope = aggregate.scopes[0]
    tree = _parse(condition)
    if tree is None:
        return True
    resolve = _definition_resolver(_aliases(metric.required_tables))
    alias_of: dict[str, str] = {}
    for alias, table in scope.tables.items():
        alias_of.setdefault(table, alias)
    rewritten = tree.copy()
    for column in list(rewritten.find_all(exp.Column)):
        physical = resolve(column)
        if physical is None or physical[0] not in alias_of:
            return True
        column.replace(exp.column(physical[1], table=alias_of[physical[0]]))
    wanted = {_canonical(c, resolve) for c in _conjuncts(tree)}
    for join in scope.select.args.get("joins") or ():
        if any(_canonical(c, scope.resolve) in wanted for c in _conjuncts(join.args.get("on"))):
            return True  # in a join: ask rather than compare a different population
    where = scope.select.args.get("where")
    kept = [c.copy() for c in _conjuncts(where.this if where is not None else None)
            if _canonical(c, scope.resolve) not in wanted]
    # NULL fails the condition too: NOT COALESCE(condition, FALSE) counts the rows it leaves out.
    excluded = exp.Not(this=exp.Coalesce(this=exp.Paren(this=rewritten), expressions=[exp.false()]))
    probe = _probe_select(scope, [], [exp.Count(this=exp.Star()).as_("excluded")])
    probe.set("where", None)
    probe = probe.where(exp.and_(*kept, excluded) if kept else excluded)
    try:
        rows = run(probe.sql(dialect="postgres"))
    except Exception as exc:  # noqa: BLE001 - unproven: ask
        logger.warning("Definition probe failed: %s", exc)
        return True
    return not rows or (rows[0].get("excluded") or 0) > 0


MAX_STATUS_VALUES = 6
# Amounts whose status policy is a business definition (cf. app.agent.ambiguity._MONEY).
_MONEY_WORDS = ("revenue", "sales", "amount", "price", "cost", "value", "spend", "paid", "payment", "fare", "income")
_STATUS = "status"


def _status_columns(aggregate: _Aggregate, registry) -> list[tuple[str, str, str]]:
    """(table, column, label) of status columns that can split this amount: a column named like a
    status, listed as a one-column dimension of the data model, on the amount's table or a table it
    joins many-to-one (a payment's order). The name only decides whether to look; which statuses
    count is the user's call."""
    if len(aggregate.tables) != 1:
        return []
    base = next(iter(aggregate.tables))
    parents = {base} | {j.to_table for j in registry.list_joins()
                        if j.from_table == base and j.relationship == "many_to_one"}
    found = []
    for (table, column), (_, label) in _dimension_columns(registry).items():
        if table in parents and _STATUS in column.lower():
            found.append((table, column, label))
    return sorted(found)


def _status_values(aggregate: _Aggregate, table: str, column: str, run: Run | None) -> list[str] | None:
    """The status values among the rows this query counts, most frequent first (the whole status
    table's when the query does not join it); None without data."""
    if run is None:
        return None
    scope = aggregate.scopes[0]
    alias = next((a for a, t in scope.tables.items() if t == table), None)
    if alias is not None and len(aggregate.scopes) == 1:
        target = exp.column(column, table=alias)
        probe = _probe_select(scope, [target.copy().as_("value")], [exp.Count(this=exp.Star()).as_("n")])
        # The status's own conditions are what is being decided: count statuses without them.
        where = scope.select.args.get("where")
        others = [c.copy() for c in _conjuncts(where.this if where is not None else None)
                  if scope.resolve(next(iter(c.find_all(exp.Column)), exp.column("_"))) != (table, column)]
        probe.set("where", None)
        if others:
            probe = probe.where(exp.and_(*others))
    else:
        # The query does not reach the status (yet): the statuses the table holds could still split it.
        target = exp.column(column, table=table)
        probe = exp.select(target.copy().as_("value"), exp.Count(this=exp.Star()).as_("n")).from_(
            exp.Table(this=exp.to_identifier(table)))
    probe.set("group", exp.Group(expressions=[target.copy()]))
    probe = probe.order_by("n DESC").limit(MAX_STATUS_VALUES + 1)
    try:
        return [str(r["value"]) for r in run(probe.sql(dialect="postgres")) if r.get("value") is not None]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status probe failed: %s", exc)
        return None


def _status_choice(metric, label: str, where: str, key: str, values: list[str], earlier: str = "") -> dict:
    """The status question: "Count every status" first, then "Only X" for each value."""
    shown = values[:MAX_STATUS_VALUES]
    options = [None] + [f"{where} = '{v}'" for v in shown]
    choices = [{"id": "all", "label": f"Count every {label}",
                "instruction": f"Count {metric.display_name} over every {label}, with no condition on {where}.",
                "constraint": {"kind": "definition", "key": key, "filter": None,
                               "label": f"Every {label} counted", "options": options}}]
    choices += [{"id": str(i), "label": f"Only {v}",
                 "instruction": f"Count {metric.display_name} only where {where} = '{v}'.",
                 "constraint": {"kind": "definition", "key": key, "filter": f"{where} = '{v}'",
                                "label": f"Only {label} {v}", "options": options}} for i, v in enumerate(shown)]
    more = " (and others)" if len(values) > MAX_STATUS_VALUES else ""
    return {"kind": "choice", "reason": "definition", "dimension": None, "rememberable": True,
            "prompt": earlier + (f"{metric.display_name} here spans several {label} values ({', '.join(shown)}{more}). "
                                 f"Nothing says which should count. Which {label} values should this answer include?"),
            "choices": choices}


def _status_definitions(aggregate: _Aggregate, metrics: list, registry, verdict: Verdict, *, decisions: dict,
                        confirmed: dict | None, run: Run | None, question: str | None = None,
                        earlier: set[str] = frozenset()) -> tuple[str | None, dict | None]:
    """(a repair, a clarification) for which statuses an amount counts. Asked only for SUM and AVG of
    money (revenue, payments, prices: not quantities or discounts), when the rows hold several
    statuses and nothing says which count. A status condition in the SQL is the model's proposal:
    only the user's answer in this conversation or the owner's confirmation authorizes one (the
    question's wording does not: "total paid" says nothing about failed payments). "Count every
    status" is the first choice."""
    if aggregate.kind not in ("SUM", "AVG") or not metrics:
        return None, None
    if not any(word in f"{m.name} {m.display_name}".lower() for m in metrics for word in _MONEY_WORDS):
        return None, None  # which statuses count is a question about money (revenue, refunds), not quantities
    metric = metrics[0]
    names = [m.name for m in metrics]
    # A status a definition's condition already covers is that definition's question, not another.
    covered = {c for m in metrics if m.base_filter for c in _definition_columns(m.base_filter, _aliases(m.required_tables))}
    for table, column, label in _status_columns(aggregate, registry):
        if (table, column) in covered:
            continue
        where = f"{table}.{column}"
        key = f"{_definition_key(aggregate)}|{where}"
        restrictions = [c for c, _ in _restrictions(aggregate, {(table, column)})]
        settled = decisions.get(key) or ((confirmed or {}).get(key))
        if settled is not None:
            chosen = settled.get("filter")
            who = "the user chose" if key in decisions else "the owner confirmed"
            target = chosen.split("= '", 1)[1].rstrip("'") if chosen and "= '" in chosen else None
            matching = [c for c in restrictions if isinstance(c, exp.EQ) and _literals(c) == [target]] if target else []
            extra = [c for c in restrictions if c not in matching]
            departs = (chosen and not matching) or extra
            # The SQL for a new question departs from an earlier answer: the user may be changing it
            # ("now count only paid orders"). Ask again, showing the earlier answer; an answer given
            # this turn, or a check with no question, is enforced instead.
            if departs and question is not None and (key in earlier or key not in decisions):
                values = _status_values(aggregate, table, column, run) or []
                for condition in restrictions:
                    values += [v for v in _literals(condition) if v not in values]
                return None, _status_choice(metric, label, where, key, values,
                                            earlier=f"Earlier, {'you chose' if key in decisions else 'the owner confirmed'}: "
                                                    f"{'only ' + label + ' ' + str(target) if target else 'every ' + label}. "
                                                    "This question may ask for something else. ")
            if chosen and not matching:
                return f"For {metric.display_name}, only {label} '{target}' counts: filter {where} = '{target}'.", None
            if extra:
                return (f"For {metric.display_name}, {who} {('only ' + label + ' ' + repr(target)) if target else 'every ' + label}: "
                        f"remove {' AND '.join(c.sql() for c in extra)}."), None
            verdict.policies.append({"name": metric.name, "metrics": names,
                                     "source": "conversation" if key in decisions else "confirmed",
                                     "statement": f"Only {label} {target}" if target else f"Every {label} counted",
                                     "conditions": [chosen] if chosen else []})
            continue
        values = _status_values(aggregate, table, column, run) or []
        for condition in restrictions:  # the model's statuses are among the options even if the probe missed them
            values += [v for v in _literals(condition) if v not in values]
        if len(values) < 2 and not restrictions:
            continue  # one status (or unknown): nothing changes
        return None, _status_choice(metric, label, where, key, values)
    return None, None


def _definitions(aggregate: _Aggregate, metrics: list, overridden: set[str], verdict: Verdict, *,
                 decided: dict | None, confirmed: dict | None, run: Run | None, question: str | None = None,
                 decided_earlier: bool = True) -> tuple[str | None, dict | None]:
    """(a repair, a clarification) for the definition the aggregate uses. The conditions of the
    matching metrics are the options, plus "no condition".

    Who decides, in order: a choice the user made in this conversation, then the owner's confirmation
    for the connection. Nothing else: the model's `definitions_overridden` and the question's wording
    are claims, not authorization ("excluding canceled" names canceled too). Without a decision the
    user is asked; with one, it is enforced. Only callers that pass no question and have nothing
    decided keep the model's claim, as before (offline use of the checks)."""
    key = _definition_key(aggregate)
    options: list[str | None] = []
    for metric in metrics:
        if (metric.base_filter or None) not in options:
            options.append(metric.base_filter or None)
    if options == [None]:
        return None, None  # every matching definition counts every row: nothing to decide
    choices = options + ([None] if None not in options else [])
    governed = {c for option in options if option for m in metrics if m.base_filter == option
                for c in _definition_columns(option, _aliases(m.required_tables))}
    chosen, source = None, None
    if decided is not None and decided.get("filter", "") in choices:
        chosen, source = decided.get("filter"), "conversation"
    elif confirmed and key in confirmed and confirmed[key].get("filter", "") in choices:
        chosen, source = confirmed[key].get("filter"), "confirmed"
    claimed = any(m.name in overridden for m in metrics if m.base_filter)
    if claimed and question is None and source is None:
        return None, None  # offline callers: the model's claim, as before (_policy records it)
    restrictions = _restrictions(aggregate, governed)
    if source is None and len([o for o in options if o]) == 1 and None not in options:
        condition = options[0]
        metric = next(m for m in metrics if m.base_filter == condition)
        resolve = _definition_resolver(_aliases(metric.required_tables))
        own = {_canonical(c, resolve) for c in _conjuncts(_parse(condition))}
        # Another restriction on the same columns (status = 'delivered') narrows the rows first: the
        # probe would then compare the wrong population, so it proves nothing. Ask.
        narrowed = any(_canonical(c, scope.resolve) not in own for c, scope in restrictions)
        if not claimed and not narrowed and not _excludes_rows(aggregate, metric, condition, run):
            verdict.policies.append({"name": metric.name, "source": "inferred", "conditions": [condition],
                                     "metrics": [m.name for m in metrics],
                                     "statement": f"{metric.display_name}: {condition} (excludes no rows here)"})
            verdict.notes.append(f"The data model's condition for {metric.display_name} ({condition}) leaves out no "
                                 "rows of this answer, so it was not asked about.")
            return "", None  # immaterial: nothing to enforce
    # The model says this question asks for something other than the decided definition: the user
    # may be changing it ("now including canceled"). Ask again rather than enforce the old answer
    # against a new request, or follow the model's reading of the words.
    # Never for an answer given this turn: that would ask the same question again.
    reask = claimed and source is not None and question is not None and (source == "confirmed" or decided_earlier)
    if source is None or reask:
        metric = next(m for m in metrics if m.base_filter) if any(m.base_filter for m in metrics) else metrics[0]
        conflict = len([o for o in options if o]) > 1 or None in options
        earlier = (f"Earlier, {'you chose' if source == 'conversation' else 'the owner confirmed'}: "
                   f"{_describe_condition(chosen)}. This question may ask for something else. ") if reask else ""
        prompt = earlier + (f"The data model has definitions of {metric.display_name} that disagree, and no one has confirmed "
                  "which is right. Which should this answer use?" if conflict else
                  f"How should {metric.display_name} be counted? The data model guessed \"{metric.base_filter}\", but no "
                  "one has confirmed it, and it changes this answer.")
        return None, {"kind": "choice", "reason": "definition", "dimension": None, "rememberable": True,
                      "prompt": prompt,
                      "choices": [{"id": str(i), "label": _describe_condition(option)
                                   + (" (the data model's guess)" if option and option == metric.base_filter and not conflict else ""),
                                   "instruction": (f"Count {metric.display_name} only where {option}." if option else
                                                   f"Count {metric.display_name} over every row, with no condition on it."),
                                   "constraint": {"kind": "definition", "key": key, "filter": option,
                                                  "label": _describe_condition(option), "options": choices}}
                                  for i, option in enumerate(choices)]}
    # Enforce the decided or confirmed definition: its condition, and no other restriction on the
    # columns it governs unless the question asks for it.
    who = "the user chose" if source == "conversation" else "the owner confirmed"
    if chosen is None:
        if restrictions:
            return (f"For {metrics[0].display_name}, {who} to count every row: remove "
                    f"{' AND '.join(c.sql() for c, _ in restrictions)}."), None
    else:
        metric = next(m for m in metrics if m.base_filter == chosen)
        resolve = _definition_resolver(_aliases(metric.required_tables))
        wanted = {_canonical(c, resolve) for c in _conjuncts(_parse(chosen))}
        present = {_canonical(condition, scope.resolve) for condition, scope in _conditions(aggregate)}
        missing = [c.sql() for c in _conjuncts(_parse(chosen)) if _canonical(c, resolve) not in present
                   and not _implied_not_null(c, resolve, aggregate)]
        if missing:
            return (f"For {metric.display_name}, {who} to count only rows where {chosen}: add {' AND '.join(missing)} "
                    f"(joining {', '.join(sorted({t.split()[0] for t in metric.required_tables}))} as needed)."), None
        extra = [c for c, scope in restrictions if _canonical(c, scope.resolve) not in wanted]
        if extra:
            return (f"For {metric.display_name}, {who} to count only rows where {chosen}: remove "
                    f"{' AND '.join(c.sql() for c in extra)}."), None
    name = next((m.name for m in metrics if (m.base_filter or None) == chosen), metrics[0].name)
    # "conditions": the definition's conditions that ran ([]: every row), so the panel never shows a
    # definition's text that assumes a different calculation.
    verdict.policies.append({"name": name, "source": source, "statement": _describe_condition(chosen),
                             "conditions": [chosen] if chosen else [],
                             "metrics": [m.name for m in metrics]})
    return "", None


def _policy(aggregate: _Aggregate, metrics: list, overridden: set[str], verdict: Verdict) -> str | None:
    if not metrics:
        return None
    governed = {t for m in metrics if m.base_filter for t, _ in
                _definition_columns(m.base_filter, _aliases(m.required_tables))}
    twice = _duplicated(aggregate, governed | set(aggregate.tables))
    if twice:
        return twice
    present = {_canonical(condition, scope.resolve) for condition, scope in _conditions(aggregate)}
    missing_by_metric = []
    for metric in metrics:
        if not metric.base_filter:
            return None  # a definition without a condition computes this
        tree = _parse(metric.base_filter)
        if tree is None:
            continue
        resolve = _definition_resolver(_aliases(metric.required_tables))
        missing = [c.sql() for c in _conjuncts(tree) if _canonical(c, resolve) not in present
                   and not _implied_not_null(c, resolve, aggregate)]
        if not missing:
            verdict.policies.append({"name": metric.name, "statement": f"{metric.display_name}: {metric.base_filter}",
                                     "source": "inferred", "conditions": [metric.base_filter],
                                     "metrics": [metric.name]})
            return None
        if metric.name in overridden:
            verdict.policies.append({"name": metric.name, "source": "question", "conditions": [],
                                     "metrics": [m.name for m in metrics],
                                     "statement": f"Not applied, as the question asks: {metric.base_filter}"})
            verdict.notes.append(f"{metric.display_name} usually means only rows where {metric.base_filter}; "
                                 "this answer does not apply that condition, as the question asks.")
            return None
        missing_by_metric.append((metric, missing))
    if not missing_by_metric:
        return None
    metric, missing = missing_by_metric[0]
    return (f"{aggregate.node.sql()} is computed from the column of {metric.display_name} ({metric.name}), whose "
            f"agreed definition only counts rows where {metric.base_filter}. Add {' AND '.join(missing)} to the "
            f"WHERE clause (joining {', '.join(sorted({t.split()[0] for t in metric.required_tables}))} as needed). "
            f"If the question explicitly asks for something else, keep your SQL and list \"{metric.name}\" in "
            "\"definitions_overridden\".")


def _probe_select(scope: _Scope, keys: list[exp.Expression], extra: list[exp.Expression]) -> exp.Select:
    select = exp.Select(expressions=[*[k.copy() for k in keys], *extra])
    for arg in ("from_", "joins", "where", "with_"):
        value = scope.select.args.get(arg)
        if value is not None:
            select.set(arg, [v.copy() for v in value] if isinstance(value, list) else value.copy())
    return select


def _unit_column(metric, registry) -> tuple[str, str, str] | None:
    try:
        dimension = registry.get_dimension(metric.unit_dimension)
    except Exception:  # noqa: BLE001
        return None
    columns = _definition_columns(dimension.sql_expression, _aliases(dimension.required_tables))
    if len(columns) != 1:
        return None
    table, column = next(iter(columns))
    return table, column, dimension.display_name


def _fixed_value(aggregate: _Aggregate, target: Physical) -> str | None:
    """The literal the query's conditions fix the column to (`currency = 'INR'`), if any."""
    for condition, scope in _conditions(aggregate):
        if isinstance(condition, exp.EQ):
            for left, right in ((condition.this, condition.expression), (condition.expression, condition.this)):
                if isinstance(left, exp.Column) and scope.resolve(left) == target and isinstance(right, exp.Literal):
                    return right.this
    return None


def _grouped_by(aggregate: _Aggregate, target: Physical) -> bool:
    scope = aggregate.scopes[0]
    return any(isinstance(k, exp.Column) and scope.resolve(k) == target for k in scope.group_keys())


def _mixed_units(aggregate: _Aggregate, metric, registry, run: Run | None, decision: dict | None
                 ) -> tuple[dict | None, str | None, str | None]:
    """(a choice clarification, a note, a repair)."""
    unit = _unit_column(metric, registry)
    if unit is None or aggregate.kind not in ("SUM", "AVG"):
        return None, None, None
    table, column, label = unit
    target = (table, column)
    fixed = _fixed_value(aggregate, target)
    grouped = _grouped_by(aggregate, target)
    if decision is not None:
        # The user chose; the SQL must do what they chose.
        wanted = decision.get("constraint") or {}
        if wanted.get("kind") == "group" and not grouped:
            return None, None, (f"The user chose to break the result down by {label}: group by {table}.{column} "
                                "as well.")
        if wanted.get("kind") == "filter" and fixed != wanted.get("value"):
            return None, None, (f"The user chose only {label} '{wanted.get('value')}': add "
                                f"{table}.{column} = '{wanted.get('value')}' to the WHERE clause.")
        return None, (f"Amounts are in {fixed}." if fixed else None), None
    if grouped:
        return None, None, None
    if fixed is not None:
        return None, f"Amounts are in {fixed}.", None
    scope = aggregate.scopes[0]
    alias = next((a for a, t in scope.tables.items() if t == table), None)
    keys = scope.group_keys()
    rows = None
    if run is not None and alias is not None and len(aggregate.scopes) == 1:
        target_column = exp.column(column, table=alias)
        probe = _probe_select(scope, keys, [exp.Count(this=exp.Distinct(expressions=[target_column.copy()])).as_("units")])
        if keys:
            probe.set("group", exp.Group(expressions=[k.copy() for k in keys]))
        try:
            rows = run(probe.limit(PROBE_ROWS).sql(dialect="postgres"))
        except Exception as exc:  # noqa: BLE001 - unproven is not single-unit
            logger.warning("Unit probe failed: %s", exc)
            rows = None
        if rows is not None and len(rows) < PROBE_ROWS and all((r.get("units") or 0) <= 1 for r in rows):
            if not keys and rows and rows[0].get("units") == 1:
                return None, f"Amounts are in a single {label}.", None
            return None, (f"Each row is in its own {label}; compare rows only within one {label}." if keys else None), None
        if rows is not None and len(rows) >= PROBE_ROWS:
            rows = None  # truncated: a group cut off here could hold a second unit
    values: list[str] = []
    if rows is not None:
        try:
            target_column = exp.column(column, table=alias)
            listing = _probe_select(scope, [target_column], [exp.Count(this=exp.Star()).as_("n")])
            listing.set("group", exp.Group(expressions=[target_column.copy()]))
            listing = listing.order_by("n DESC").limit(MAX_UNIT_CHOICES)
            values = [str(r.get(column)) for r in run(listing.sql(dialect="postgres")) if r.get(column) is not None]
        except Exception:  # noqa: BLE001 - the breakdown choice still works
            values = []
    choices = [{"id": BREAK_DOWN, "label": f"Break down by {label}",
                "instruction": f"Group the result by {table}.{column} as well, so no figure mixes {label} values.",
                "constraint": {"kind": "group"}}]
    choices += [{"id": str(i), "label": value, "instruction": f"Only include rows where {table}.{column} = '{value}'.",
                 "constraint": {"kind": "filter", "value": value}} for i, value in enumerate(values)]
    prompt = (f"{metric.display_name} is recorded in several {label} values here, so one figure across them "
              f"would be meaningless. Pick one {label}, or break the result down by {label}." if rows is not None else
              f"{metric.display_name} may be recorded in several {label} values, and I could not check which ones "
              f"this question covers. Break the result down by {label}?")
    return {"kind": "choice", "reason": "mixed_units", "dimension": metric.unit_dimension, "prompt": prompt,
            "choices": choices}, None, None


def _is_zero(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and float(node.this) == 0


def _excludes_zeros(aggregate: _Aggregate, column: Physical) -> bool:
    """NULLIF(column, 0) in the aggregate, or a condition column <> 0 / column > 0 / 0 < column."""
    scope = aggregate.scopes[0]
    argument = aggregate.argument
    while isinstance(argument, exp.Cast):
        argument = argument.this
    # Only the whole argument: COALESCE(NULLIF(rating, 0), 0) puts the zeros back.
    if isinstance(argument, exp.Nullif) and isinstance(argument.this, exp.Column) \
            and scope.resolve(argument.this) == column and _is_zero(argument.expression):
        return True
    for condition, owner in _conditions(aggregate):
        if isinstance(condition, (exp.NEQ, exp.GT)) and isinstance(condition.this, exp.Column) \
                and owner.resolve(condition.this) == column and _is_zero(condition.expression):
            return True
        if isinstance(condition, (exp.NEQ, exp.LT)) and isinstance(condition.expression, exp.Column) \
                and owner.resolve(condition.expression) == column and _is_zero(condition.this):
            return True
    return False


def _zero_placeholder(aggregate: _Aggregate, metric, registry, decision: dict | None
                      ) -> tuple[dict | None, str | None]:
    """(a choice clarification, a repair)."""
    if aggregate.kind != "AVG" or len(aggregate.columns) != 1:
        return None, None
    variant = next((m for m in registry.schema.metrics if getattr(m, "excludes_zeros_of", None) == metric.name), None)
    if variant is None:
        return None, None
    table, column = next(iter(aggregate.columns))
    excluded = _excludes_zeros(aggregate, (table, column))
    if decision is not None:
        wanted = (decision.get("constraint") or {}).get("kind")
        if wanted == "exclude" and not excluded:
            return None, f"The user chose to leave out rows where {table}.{column} = 0: add {table}.{column} <> 0."
        if wanted == "keep" and excluded:
            return None, f"The user said 0 is a real value of {table}.{column}: keep those rows."
        return None, None
    if excluded:
        return None, None
    share = f"{variant.zero_share:.0%} of rows" if variant.zero_share else "many rows"
    return {"kind": "choice", "reason": "zero_placeholder", "dimension": None,
            "prompt": (f"{metric.display_name} is 0 in {share}. If 0 means \"not recorded\" there, averaging those "
                       "rows in pulls the figure down. Leave them out?"),
            "choices": [{"id": ZERO_EXCLUDE, "label": "Leave out the zeros (0 means not recorded)",
                         "instruction": f"Leave out rows where {table}.{column} = 0: there 0 means not recorded.",
                         "constraint": {"kind": "exclude"}},
                        {"id": ZERO_INCLUDE, "label": "Keep them (0 is a real value)",
                         "instruction": f"Keep rows where {table}.{column} = 0: 0 is a real value.",
                         "constraint": {"kind": "keep"}}]}, None


def _dimension_columns(registry) -> dict[Physical, tuple[str, str]]:
    """(table, column) -> (dimension name, label), for dimensions that are one plain column."""
    out = {}
    for dimension in registry.schema.dimensions:
        if getattr(dimension, "separator", None):
            continue  # list values ("North Indian, Chinese"): matched by item, not by '='
        tree = _parse(dimension.sql_expression)
        if isinstance(tree, exp.Column):
            found = _definition_resolver(_aliases(dimension.required_tables))(tree)
            if found:
                out[found] = (dimension.name, dimension.display_name)
    return out


def _filters_answer_rows(scope: _Scope) -> bool:
    """Whether a scope's conditions restrict the rows the answer is computed from: the query itself,
    its CTEs and derived tables, `col IN (subquery)`, and a correlated EXISTS. An uncorrelated
    EXISTS only asks whether some row exists anywhere."""
    if not scope.scope.is_subquery:
        return True
    parent = scope.select.parent
    while isinstance(parent, (exp.Subquery, exp.Paren)):
        parent = parent.parent
    if isinstance(parent, exp.In):
        return True
    return isinstance(parent, exp.Exists) and scope.scope.is_correlated_subquery


def _literal_conditions(scopes: list[_Scope]):
    """(scope, column, literal) for each `column = 'text'` and `column IN ('a', 'b')`."""
    for scope in scopes:
        for condition in scope.where():
            if isinstance(condition, exp.EQ):
                for left, right in ((condition.this, condition.expression), (condition.expression, condition.this)):
                    if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
                        yield scope, left, right
            elif isinstance(condition, exp.In) and isinstance(condition.this, exp.Column):
                for item in condition.expressions:
                    if isinstance(item, exp.Literal) and item.is_string:
                        yield scope, condition.this, item


def _choose(label: str, where: str, raw: str, dimension: str, options: list[str]) -> Verdict:
    choices = [{"id": str(i), "label": option,
                "instruction": f"For '{raw}', use {where} = '{option}'.",
                "constraint": {"kind": "value", "column": where, "raw": raw, "value": option}}
               for i, option in enumerate(options[:MAX_UNIT_CHOICES])]
    choices.append({"id": "any", "label": f"Any {label}",
                    "instruction": f"Do not filter {label} on '{raw}'.",
                    "constraint": {"kind": "any", "column": where, "raw": raw, "value": None}})
    return Verdict(reason="no_match", clarification={
        "kind": "choice", "reason": "no_match", "dimension": dimension, "raw": raw,
        "prompt": f"Which {label.lower()} did you mean? None is called '{raw}'.", "choices": choices})


def _probe_value(scope: _Scope, column: exp.Column, physical: Physical, raw: str, run: Run) -> list[str] | None:
    """For a column the value index does not cover: None when some row has the value; otherwise the
    most frequent values containing it, case-insensitively (possibly none)."""
    table, name = physical
    target = exp.column(name, table=table)
    source = exp.Table(this=exp.to_identifier(table))
    try:
        hit = exp.select("1 AS hit").from_(source).where(exp.EQ(this=target.copy(), expression=exp.Literal.string(raw)))
        if run(hit.limit(1).sql(dialect="postgres")):
            return None
        if "%" in raw or "_" in raw:
            return []  # would be a wildcard in LIKE, not the user's character
        pattern = exp.Literal.string(f"%{raw.lower()}%")
        close = (exp.select(target.copy().as_("value"), "COUNT(*) AS n").from_(source)
                 .where(exp.Like(this=exp.Lower(this=target.copy()), expression=pattern))
                 .group_by(target.copy()).order_by("n DESC").limit(MAX_UNIT_CHOICES))
        return [str(r["value"]) for r in run(close.sql(dialect="postgres")) if r.get("value") is not None]
    except Exception as exc:  # noqa: BLE001 - grounding must never break a question
        logger.warning("Value probe failed for %s: %s", physical, exc)
        return None


def _values(scopes: list[_Scope], registry, index, decisions: list[dict], run: Run | None = None) -> Verdict | None:
    if index is None and run is None:
        return None
    by_column = _dimension_columns(registry)
    chosen = {(d["constraint"].get("column"), d["constraint"].get("raw")): d["constraint"]
              for d in decisions if d.get("reason") == "no_match" and isinstance(d.get("constraint"), dict)}
    effective = [scope for scope in scopes if _filters_answer_rows(scope)]
    present = {(f"{p[0]}.{p[1]}", literal.this) for scope, column, literal in _literal_conditions(effective)
               if (p := scope.resolve(column))}
    for (where, raw), constraint in chosen.items():
        # The choice must be in the SQL as chosen, where it filters the answer's rows: the value
        # present, not replaced by another value or dropped; "any" means no value filter on that column.
        target = constraint.get("value")
        if constraint.get("kind") == "any":
            others = sorted(v for w, v in present if w == where)
            if others:
                return Verdict(reason="no_match", repair=(
                    f"The user chose any value of {where} for '{raw}': remove the condition {where} = '{others[0]}'."))
            continue
        if constraint.get("kind") == "value" and (where, target) not in present:
            return Verdict(reason="no_match", repair=(
                f"The user chose '{target}' for '{raw}': the query must keep {where} = '{target}'."))
        if constraint.get("kind") == "value" and any(w == where and v != target for w, v in present):
            return Verdict(reason="no_match", repair=(
                f"The user chose '{target}' for '{raw}': filter {where} on '{target}' only."))
    for scope, column, literal in _literal_conditions(scopes):
        physical = scope.resolve(column)
        if physical is None:
            continue
        raw, where = literal.this, f"{physical[0]}.{physical[1]}"
        dimension, label = by_column.get(physical) or (physical[1], physical[1].replace("_", " ").title())
        decided = chosen.get((where, raw))
        if decided is not None:
            target = decided.get("value")
            return Verdict(reason="no_match", repair=(
                f"The user chose {label} '{target}' for '{raw}': write {where} = '{target}'." if target else
                f"The user chose not to filter {label} on '{raw}': remove that condition."))
        if physical not in by_column or dimension not in getattr(index, "dimensions", ()):
            if run is None or not raw.strip():
                continue
            options = _probe_value(scope, column, physical, raw, run)
            if options:
                return _choose(label, where, raw, dimension, options)
            continue
        try:
            snapped = index.snap(dimension, raw)
        except Exception as exc:  # noqa: BLE001 - grounding must never break a question
            logger.warning("Value check failed for %s: %s", dimension, exc)
            continue
        if snapped.confidence in ("exact", "agree") and snapped.value is not None:
            if snapped.value != raw:
                return Verdict(reason="value", repair=(
                    f"{where} = '{raw}' matches nothing: {label} is stored as '{snapped.value}'. Use that value."))
            continue
        if snapped.confidence == "weak" and snapped.alternatives:
            return _choose(label, where, raw, dimension, snapped.alternatives)
    return None


def check_sql(sql: str, registry, columns_by_table: dict[str, set[str]], *, run: Run | None = None,
              overridden: set[str] | frozenset[str] = frozenset(), decisions: list[dict] | None = None,
              values=None, ask_definitions: bool = False, confirmed: dict | None = None,
              question: str | None = None) -> Verdict:
    """The verdict on one query (logical table names, as the model writes them). `decisions`: the
    user's answers this turn ({reason, constraint}); the SQL must satisfy them. `values`: the
    connection's value index (app.grounding.values.ValueIndex), for the value check."""
    decisions = [d for d in decisions or () if isinstance(d, dict) and d.get("reason")]
    by_reason = {d["reason"]: d for d in decisions}
    by_definition = {d["constraint"].get("key"): d["constraint"] for d in decisions
                     if d["reason"] == "definition" and isinstance(d.get("constraint"), dict)}
    # Definitions answered on an earlier turn (the node marks them); this turn's answers are final.
    earlier_keys = {d["constraint"].get("key") for d in decisions
                    if d["reason"] == "definition" and d.get("earlier") and isinstance(d.get("constraint"), dict)}
    verdict = Verdict()
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        scopes = [_Scope(s, columns_by_table) for s in traverse_scope(tree) if isinstance(s.expression, exp.Select)]
    except Exception as exc:  # noqa: BLE001
        return Verdict(repair=f"the query could not be checked ({str(exc)[:120]}); write it as plain SELECTs "
                              "with joins, subqueries or CTEs", reason="unchecked")
    found = _values(scopes, registry, values, decisions, run)
    if found is not None:
        return found
    for scope in scopes:
        aggregates = scope.aggregates()
        unproven = False
        if aggregates:
            shape, unproven = _join_shape(scope, registry)
            if shape:
                return Verdict(reason="fan_out", repair=shape)
        for aggregate in aggregates:
            if not aggregate.traced:
                return Verdict(reason="unchecked", repair=(
                    f"cannot tell which table's column {aggregate.node.sql()} aggregates (an ambiguous name, a "
                    "UNION or a window function in between). Qualify every column with its table or alias, and "
                    "select the columns a subquery passes on by name."))
            if aggregate.distinct and aggregate.kind in ("SUM", "AVG"):
                return Verdict(reason="distinct_amounts", repair=(
                    f"{aggregate.node.sql()} drops equal amounts (two rows can have the same value) and does not "
                    "remove repeated rows. Remove DISTINCT; if a join repeats rows, aggregate per key in a "
                    "subquery first."))
            if aggregate.aggregated:
                # Checked in the subquery that aggregated it; here only whether the join repeats it.
                problem = _rejoined(aggregate, scope, run)
                if problem:
                    return Verdict(repair=problem, reason="fan_out")
                continue
            problem = _fan_out(aggregate, registry, run, verdict, unproven=unproven)
            if problem:
                return Verdict(repair=problem, reason="fan_out")
            exact, related = _matching_metrics(aggregate, registry)
            for metric in exact:
                verdict.measures.append({"name": metric.name, "label": metric.display_name,
                                         "aggregation": metric.aggregation, "sql": aggregate.node.sql()})
            if related and not exact:
                verdict.notes.append(f"{aggregate.node.sql()} uses the column of {related[0].display_name} but is "
                                     "not its definition.")
            metrics = exact + related
            problem = None
            decided_here = False
            if ask_definitions and metrics:
                problem, choice = _definitions(aggregate, metrics, set(overridden), verdict,
                                               decided=by_definition.get(_definition_key(aggregate)),
                                               confirmed=confirmed, run=run, question=question,
                                               decided_earlier=_definition_key(aggregate) in earlier_keys)
                if choice:
                    return Verdict(clarification=choice, reason="definition", measures=verdict.measures)
                if problem:
                    return Verdict(repair=problem, reason="definition")
                decided_here = problem == ""
                if not any(m.base_filter for m in metrics) or decided_here:
                    status_problem, choice = _status_definitions(
                        aggregate, metrics, registry, verdict, decisions=by_definition, confirmed=confirmed, run=run,
                        question=question, earlier=earlier_keys)
                    if choice:
                        return Verdict(clarification=choice, reason="definition", measures=verdict.measures)
                    if status_problem:
                        return Verdict(repair=status_problem, reason="definition")
            if decided_here:
                governed = {t for m in metrics if m.base_filter for t, _ in
                            _definition_columns(m.base_filter, _aliases(m.required_tables))}
                problem = _duplicated(aggregate, governed | set(aggregate.tables))
            else:
                problem = _policy(aggregate, metrics, set(overridden), verdict)
            if problem:
                return Verdict(repair=problem, reason="policy")
            for metric in exact + related:
                if metric.unit_dimension:
                    choice, note, repair = _mixed_units(aggregate, metric, registry, run, by_reason.get("mixed_units"))
                    if repair:
                        return Verdict(repair=repair, reason="mixed_units")
                    if choice:
                        return Verdict(clarification=choice, reason="mixed_units", measures=verdict.measures)
                    if note and note not in verdict.notes:
                        verdict.notes.append(note)
                choice, repair = _zero_placeholder(aggregate, metric, registry, by_reason.get("zero_placeholder"))
                if repair:
                    return Verdict(repair=repair, reason="zero_placeholder")
                if choice:
                    return Verdict(clarification=choice, reason="zero_placeholder", measures=verdict.measures)
    return verdict


_OPERATORS = {"neq": "ne"}  # sqlglot's names to the panel's, where they differ


def describe(sql: str, registry, columns_by_table: dict[str, set[str]]) -> dict:
    """What the outer query computes, for the interpretation panel: its conditions, grouping, order
    and limit, read from the SQL itself."""
    tree = sqlglot.parse_one(sql, read="postgres")
    scope = _Scope(traverse_scope(tree)[-1], columns_by_table) if isinstance(tree, exp.Select) else None
    if scope is None:
        return {"filters": [], "grouping": [], "order_by": None, "limit": None, "partial": True}
    # Conditions in subqueries, HAVING and window functions are not listed: say so, and show the SQL.
    partial = (len(traverse_scope(tree)) > 1 or scope.select.args.get("having") is not None
               or tree.find(exp.Window) is not None)
    labels = {}
    for dimension in registry.schema.dimensions:
        columns = _definition_columns(dimension.sql_expression, _aliases(dimension.required_tables))
        if len(columns) == 1:
            labels[".".join(next(iter(columns))).lower()] = dimension.display_name
    filters = []
    for condition in scope.where():
        left = getattr(condition, "this", None)
        if isinstance(condition, exp.EQ) and isinstance(left, exp.Column) and isinstance(condition.expression, exp.Column):
            continue  # a join key, not a condition on the data
        if isinstance(condition, exp.Binary) and isinstance(left, exp.Column):
            name = _canonical(left, scope.resolve)
            right = condition.expression
            filters.append({"dimension": name, "label": labels.get(name, left.name.replace("_", " ")),
                            "operator": _OPERATORS.get(condition.key, condition.key),
                            "value": right.this if isinstance(right, exp.Literal) else right.sql()})
        else:
            filters.append({"dimension": "condition", "label": "Condition", "operator": "sql", "value": condition.sql()})
    grouping = []
    for key in scope.group_keys():
        name = _canonical(key, scope.resolve)
        grouping.append({"name": name, "label": labels.get(name, key.sql())})
    order = scope.select.args.get("order")
    limit = scope.select.args.get("limit")
    limit_value = limit.expression if limit is not None else None
    return {"filters": filters, "grouping": grouping, "order_by": order.sql() if order is not None else None,
            "partial": partial,
            "limit": int(limit_value.this) if isinstance(limit_value, exp.Literal) and limit_value.is_int else None}
