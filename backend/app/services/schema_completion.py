"""Turn an LLM's schema proposal into a full SemanticSchema, deterministically (Phase 3e).

A small local model (qwen3:4b) could not write a whole SemanticSchema: the JSON ran past its output
budget, and it left out fields the database already answers (tables, joins, which tables each
expression needs). So the LLM only proposes what needs judgment (metrics, dimensions, time
dimensions with their SQL expressions) and this module derives the rest from introspection:

- tables and joins come from the connector's schema;
- every column reference in an expression is resolved against the real columns: `op.amount`
  (an alias nobody declared), `orders.amount` (wrong table) and a bare `amount` all become
  `<table>.amount` when exactly one table has that column (or one of several is already in use);
- required tables are the tables an expression references, the expression's own table first
  (the SQL builder treats the first as the query's base table).

Items whose references cannot be resolved are dropped here; the execution probes catch the rest.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.connectors.base import SchemaContext
from app.semantic.models import (SemanticDimension, SemanticFilterColumn, SemanticJoin, SemanticMetric, SemanticSchema, SemanticTable,
                                 SemanticTimeDimension)

logger = logging.getLogger(__name__)


class ProposedMetric(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    aggregation: Literal["SUM", "AVG", "COUNT_DISTINCT", "COUNT", "RATIO"]
    sql_expression: str | None = None
    numerator_sql: str | None = None
    denominator_sql: str | None = None
    base_filter: str | None = None


class ProposedDimension(BaseModel):
    name: str
    display_name: str | None = None
    sql_expression: str
    cardinality: Literal["low", "medium", "high"] = "medium"


class ProposedTimeDimension(BaseModel):
    name: str
    display_name: str | None = None
    sql_expression: str
    default_granularity: Literal["day", "week", "month", "quarter", "year"] = "month"


class SchemaProposal(BaseModel):
    """What the LLM writes. Everything else in a SemanticSchema is derived."""
    dataset: str | None = None
    description: str | None = None
    metrics: list[ProposedMetric] = Field(default_factory=list)
    dimensions: list[ProposedDimension] = Field(default_factory=list)
    time_dimensions: list[ProposedTimeDimension] = Field(default_factory=list)


_QUOTED = re.compile(r"'(?:[^']|'')*'")
_QUALIFIED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_IDENTIFIER = re.compile(r"(?<![.\w])([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\(|\.)")


class _Resolver:
    def __init__(self, ctx: SchemaContext):
        self.columns = {table: {c["name"] for c in cols} for table, cols in ctx.tables.items()}
        self.owners: dict[str, list[str]] = {}
        for table, cols in self.columns.items():
            for column in cols:
                self.owners.setdefault(column, []).append(table)

    def resolve(self, expression: str) -> tuple[str, list[str]] | None:
        """(expression with every column qualified by its table, tables in order of first use);
        None when a reference cannot be resolved. A constant ("1") resolves to no tables."""
        used: list[str] = []
        # Work on the expression with string literals masked, so 'a.b' inside quotes is left alone.
        literals: list[str] = []

        def mask(match: re.Match) -> str:
            literals.append(match.group(0))
            return f"\x00{len(literals) - 1}\x00"

        masked = _QUOTED.sub(mask, expression)
        failed = False

        def owner(column: str, hint: str | None) -> str | None:
            tables = self.owners.get(column, [])
            if hint in tables:
                return hint
            if len(tables) == 1:
                return tables[0]
            in_use = [t for t in tables if t in used]
            if len(in_use) == 1:
                return in_use[0]
            # An alias made of the table's initials ("op" for order_payments).
            initials = [t for t in tables if hint and "".join(w[0] for w in t.split("_")) == hint]
            return initials[0] if len(initials) == 1 else None

        def qualified(match: re.Match) -> str:
            nonlocal failed
            qualifier, column = match.group(1), match.group(2)
            table = owner(column, qualifier)
            if table is None:
                failed = True
                return match.group(0)
            if table not in used:
                used.append(table)
            return f"{table}.{column}"

        masked = _QUALIFIED.sub(qualified, masked)

        def bare(match: re.Match) -> str:
            nonlocal failed
            word = match.group(1)
            if word not in self.owners or word in self.columns:  # keywords, functions, table names
                return word
            table = owner(word, None)
            if table is None:
                failed = True
                return word
            if table not in used:
                used.append(table)
            return f"{table}.{word}"

        # Bare names: only outside already-qualified references (the regex skips "x.col" and "col(").
        masked = _IDENTIFIER.sub(bare, masked)
        if failed:
            return None
        for i, literal in enumerate(literals):
            masked = masked.replace(f"\x00{i}\x00", literal)
        return masked, used


# Sized for multi-table databases (Chinook: 11 tables); a single file stays well below both.
MAX_METRICS = 24
MAX_DIMENSIONS = 20
# Northwind (20 tables): the LLM's 20 dimensions left no room for product category, customer
# company or employee name. One more slot per table beyond 10, up to 30.
MAX_DIMENSIONS_LARGE = 30


def _dimension_cap(ctx: SchemaContext) -> int:
    return min(MAX_DIMENSIONS_LARGE, MAX_DIMENSIONS + max(0, len(ctx.tables) - 10))


# Per-row text and contact details, never a breakdown whatever their cardinality (Northwind's 9
# employees have 9 notes and 9 web pages; the LLM proposed both).
_NOT_A_BREAKDOWN = re.compile(r"(^|_)(notes?|comments?|remarks?|attachments?|web_page|website|url|email|e_mail|fax|"
                              r"phone|mobile|address)($|_)")
# Money that flows (fees, taxes, quantities) adds up however few distinct values it takes: Northwind's
# shipping fees take 13 values, and "total shipping fees" had only an average.
_FLOW = re.compile(r"(amount|revenue|sales|cost|profit|quantity|qty|units|freight|fee|tax|spend|balance)")
# Counts of the largest tables only: on Pagila counts of all 11 tables (languages, countries...)
# took the slots of measures, and "average rental rate" had no metric.
MAX_ENTITY_COUNTS = 8
MAX_TIME_DIMENSIONS = 6
_AUDIT_TIME = re.compile(r"(^|_)(last_update|last_updated|updated|updated_at|modified|modified_at|last_modified)$")
_NUMERIC = ("INT", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL")
# TPC-H names keys c_custkey / o_orderkey, the MySQL employees database emp_no / dept_no.
# Uploads lowercase headers, so VendorID arrives as "vendorid": a fused "...id" counts too, unless
# the word is one (paid, valid, rapid, acid, humid).
_ID_LIKE = re.compile(r"(^id$|_id$|[a-z0-9]{2}(?<![alpcm])id$|_ref$|_code$|key$|_no$|_num$|zip|prefix|_seq|"
                      r"sequential|^year$)")
# Coordinates and contact details: numbers or strings, but never a measure or a useful breakdown.
_NEVER_MEASURE = re.compile(r"(^|_)(lat|latitude|lng|lon|long|longitude|phone|zip|postcode)($|_)")
# Codes stored as numbers: their sum or average means nothing (the taxi upload's payment_type, 1-5,
# became "Average Payment Type"). Only for measures: such columns are good breakdowns.
_CODE_COLUMN = re.compile(r"(^|_)(type|code|flag|status|kind|class|category|categ)($|_)")
_MEASURE_WORDS = re.compile(r"(amount|price|revenue|sales|value|cost|profit|total|quantity|qty|units|"
                            r"weight|freight|fee|discount|tax|margin|spend|budget|balance)")
# A score or a level: only its average means something ("total rating", "total price range" do not).
# Per-unit figures too, by their renamed CSV headers: Price/Earnings -> price_earnings, Earnings/Share
# -> earnings_share, 52 Week High -> c_52_week_high (the S&P file got "Total Price/Earnings").
_NOT_SUMMABLE = re.compile(r"(rating|score|stars|rank|percent|pct|ratio|rate|range|level|tier|grade|band|class|yield|"
                           r"(^|_)per_|_per($|_)|_to_|(^|_)price_(earnings|book|sales|cash)|_share$|(^|_)(high|low)$|"
                           r"52_week)")
_SUMMABLE_MIN_DISTINCT = 20  # fewer distinct values: a score or a code, where only the average means something
# Free text or locations: high-cardinality strings that are not names to rank by.
_FREE_TEXT = re.compile(r"(address|street|description|comment|message|text|review|note|url|email|verbose)")
_FREE_TEXT_MIN_DISTINCT = 50
_ENTITY_NAME = re.compile(r"(^|_)(name|title)$")
_DIMENSION_MAX_DISTINCT = 500
_NUMERIC_DIMENSION_MAX_DISTINCT = 20
# Contact details: per-person values, never a breakdown (an employee table's 8 faxes and emails
# filled the dimension cap ahead of customer country).
_CONTACT = re.compile(r"(^|_)(email|e_mail|fax|phone|mobile|address|street|postal|postcode|zip|url|website)($|_)")
# Names that say nothing without their table: genre.name, album.title, employee.title.
_GENERIC_COLUMN = {"name", "title", "type", "status", "code", "category", "description", "label", "kind"}
_GENERIC_MEASURE = {"total", "amount", "value", "price", "unit_price", "quantity", "cost"}
_JOIN_COLUMN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def _join_columns(ctx: SchemaContext) -> set[tuple[str, str]]:
    """(table, column) pairs used as join keys: identifiers, never measures (employee.reports_to)."""
    return {(m.group(1), m.group(2)) for j in list(ctx.join_paths) + list(ctx.inferred_joins)
            for m in _JOIN_COLUMN.finditer(str(j.get("on", "")))} | set(getattr(ctx, "key_columns", ()))


def _shared_columns(ctx: SchemaContext) -> set[str]:
    """Column names (snake_case) found in more than one table: customer.city and employee.city.
    Their dimensions and metrics always carry the table's name: the first table to claim plain
    "city" was the employees' (3 values), and "customers per city" was answered with it."""
    seen: dict[str, int] = {}
    for columns in ctx.tables.values():
        for name in {_snake(c["name"]) for c in columns}:
            seen[name] = seen.get(name, 0) + 1
    return {name for name, n in seen.items() if n > 1}


_PRICE_COLUMN = re.compile(r"(^|_)(unit_price|price)$")
_QUANTITY_COLUMN = re.compile(r"(^|_)(quantity|qty|units)$")
# A product's stock settings are no quantity sold: Northwind got "Total Products Amount" =
# SUM(list_price x minimum_reorder_quantity).
_STOCK_SETTING = re.compile(r"(reorder|minimum|maximum|min_|max_|target|stock|on_hand|level)")


def _line_amount_metrics(ctx: SchemaContext, metrics: list[SemanticMetric], seen: set[str],
                         notes: list[str]) -> list[SemanticMetric]:
    """SUM(price x quantity) for a table with both (order lines, invoice lines): the amount at the
    line's own level. Without it, "revenue by genre" could only sum invoice totals over lines,
    counting an invoice once per line."""
    added: list[SemanticMetric] = []
    covered = {(m.sql_expression or "").replace(" ", "").lower() for m in metrics}
    for table, cols in ctx.tables.items():
        price = next((c["name"] for c in cols if _PRICE_COLUMN.search(c["name"].lower()) and _is_numeric(c)), None)
        quantity = next((c["name"] for c in cols if _QUANTITY_COLUMN.search(c["name"].lower()) and _is_numeric(c)
                         and not _STOCK_SETTING.search(c["name"].lower())), None)
        if not price or not quantity or len(metrics) + len(added) >= MAX_METRICS:
            continue
        expression = f"{table}.{price} * {table}.{quantity}"
        name = f"total_{_snake(table)}_amount"
        if expression.replace(" ", "").lower() in covered or name in seen:
            continue
        seen.add(name)
        label = f"Total {_title(table)} Amount"
        # "Sales" in the description: on Northwind "total sales" and "sales by employee" found no
        # metric under the name "Total Order Details Amount" and were refused.
        added.append(SemanticMetric(name=name, display_name=label,
                                    description=f"Sales (revenue): sum of {_title(price)} x {_title(quantity)} per "
                                                f"{_title(table)} line (added for coverage)",
                                    aggregation="SUM", sql_expression=expression, required_tables=[table]))
        notes.append(f"added metric {name}")
    return added


def _link_tables(ctx: SchemaContext) -> set[str]:
    """Tables made only of join keys (and timestamps) with at least two keys out: many-to-many links
    such as film_category or playlist_track."""
    keys = _join_columns(ctx)
    outgoing: dict[str, int] = {}
    for join in list(ctx.join_paths) + list(ctx.inferred_joins):
        if join.get("relationship") == "many_to_one" and join.get("from") != join.get("to"):
            outgoing[join["from"]] = outgoing.get(join["from"], 0) + 1
    return {table for table, columns in ctx.tables.items()
            if outgoing.get(table, 0) >= 2 and columns
            and all((table, c["name"]) in keys or _is_temporal(c) for c in columns)}


def _plural(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    return word + ("es" if re.search(r"(s|x|z|ch|sh)$", word) else "s")


_FILE_SUFFIX = re.compile(r"(data|table|tbl|records|info|details|raw|export|dump)$")


def _row_noun(table: str) -> str:
    """What a row of a file is called, from its name: yellow_tripdata_2024_01 -> trip. The count
    was named "yellow_tripdata_2024_01s", and "how many trips" took total_trip_distance instead
    (benchmark). Dates, numbers and words like "data" are dropped; the last word is the noun."""
    words = [w for w in _snake(table).split("_") if w and not w.isdigit()]
    if not words:
        return _snake(table)
    noun = _FILE_SUFFIX.sub("", words[-1]) or (words[-2] if len(words) > 1 else words[-1])
    return noun if len(noun) >= 3 else _snake(table)


def _singular(stem: str) -> str:
    if stem.endswith("ies"):
        return re.sub(r"(?<=[^aeiou])ies$", "y", stem)
    if re.search(r"(ss|us|is)$", stem):  # address, status, analysis: already singular
        return stem
    return re.sub(r"s$", "", stem)


def _table_key(ctx: SchemaContext, table: str, cols: list[dict]) -> str | None:
    """The column identifying one row of the table: "<table>_id" or "id" first, then any ID-like
    column (c_custkey, dept_no) unique enough to be the key. Distinct counts can be approximate (off
    by up to ~25%), so "unique enough" is three quarters of the rows."""
    by_snake = {_snake(c["name"]): c["name"] for c in cols}
    stem = _snake(table)
    singular = _singular(stem)
    rows, distinct = ctx.row_counts.get(table, -1), ctx.distinct_counts.get(table, {})

    def unique(column: str) -> bool:
        return rows <= 0 or distinct.get(column, -1) < 0 or distinct[column] >= 0.75 * rows

    named = by_snake.get(f"{stem}_id") or by_snake.get(f"{singular}_id") or by_snake.get("id")
    if named is not None:
        # Not the table's key when far from unique: Olist's order_item_id numbers the items within an order (1-21).
        return named if rows <= 0 or distinct.get(named, -1) < 0 or distinct[named] >= 0.5 * rows else None
    keys = [c["name"] for c in cols if _ID_LIKE.search(_snake(c["name"])) and not _is_temporal(c)
            and rows > 0 and distinct.get(c["name"], -1) >= 0 and unique(c["name"])]
    return keys[0] if len(keys) == 1 else None


def _entity_count_metrics(ctx: SchemaContext, metrics: list[SemanticMetric], seen: set[str],
                          notes: list[str]) -> list[SemanticMetric]:
    """A count for each table: of its key ("number of customers") or, for a table with no single
    key (order lines, trips, salary history), of its rows. On Chinook the LLM proposed none, and
    "tracks by genre" was answered with the quantity sold; on the benchmark's taxi upload "how many
    trips" had no count at all and was answered with total fares. Counts do not compete for the
    MAX_METRICS slots (the LLM's metrics had filled them): only the MAX_ENTITY_COUNTS largest tables
    get one. Link tables (playlist_track: only others' IDs) get none."""
    covered = {(m.sql_expression or "").strip() for m in metrics if m.aggregation in ("COUNT", "COUNT_DISTINCT")}
    columns = {_snake(c["name"]) for cols in ctx.tables.values() for c in cols}
    links = _link_tables(ctx)
    added: list[SemanticMetric] = []
    by_size = sorted(ctx.tables.items(), key=lambda item: -ctx.row_counts.get(item[0], 0))
    for table, cols in by_size:
        if len(added) >= MAX_ENTITY_COUNTS:
            break
        if table in links:
            continue
        key = _table_key(ctx, table, cols)
        if key is not None and f"{table}.{key}" in covered:
            continue
        if key is None and not cols:
            continue
        if key is None and all(_ID_LIKE.search(_snake(c["name"])) or _is_temporal(c) for c in cols):
            continue  # only other tables' keys: a link table (playlist_track) even without declared joins
        stem = _snake(table) if key is not None else _row_noun(table)
        name = stem if _singular(stem) != stem else _plural(stem)  # "orders" stays; "track" -> "tracks"
        if key is None and len(ctx.tables) == 1:
            name = "records"  # a single file without IDs (the S&P file): each row is one record
        if name in seen or name in columns:
            name = f"number_of_{name}"
            if name in seen:
                continue
        if name in seen:
            continue
        seen.add(name)
        label = f"Number of {_title(name)}"
        if key is not None:
            added.append(SemanticMetric(name=name, display_name=label, description=f"{label} (added for coverage)",
                                        aggregation="COUNT_DISTINCT", sql_expression=f"{table}.{key}",
                                        required_tables=[table]))
        else:
            # Each row is one. COUNT over a column unique to each row when there is one (the S&P
            # file's company name), else over a key column (l_orderkey, vendor_id: never empty),
            # else the first column: metric expressions stay "table.column".
            rows = ctx.row_counts.get(table, -1)
            unique = [c["name"] for c in cols if not _is_numeric(c) and rows > 0
                      and ctx.distinct_counts.get(table, {}).get(c["name"], -1) >= 0.95 * rows]
            row_key = next((c for c in unique if _ENTITY_NAME.search(c.lower())), unique[0] if unique else None)
            what = f"each row is one {_title(row_key)}" if row_key else f"one per row of {table}"
            column = row_key or next((c["name"] for c in cols if _ID_LIKE.search(_snake(c["name"]))), cols[0]["name"])
            if f"{table}.{column}" in covered:
                continue  # this row count exists already (a count of some other column is not one:
                # the taxi model's COUNT(passenger_count) "total_passenger_count" had hidden the trip count)
            added.append(SemanticMetric(name=name, display_name=label, description=f"{label}: {what} (added for coverage)",
                                        aggregation="COUNT", sql_expression=f"{table}.{column}", required_tables=[table]))
        notes.append(f"added metric {name}")
    return added


def _measure_metrics(ctx: SchemaContext, metrics: list[SemanticMetric], seen: set[str],
                     notes: list[str]) -> list[SemanticMetric]:
    """SUM/AVG metrics for numeric measure columns the LLM left out, up to MAX_METRICS in total.

    A 4B model proposed 3 metrics for a retail file with quantity and unit_price columns; "units
    sold" was then answered with an order count. Covering every measure column lets the agent
    answer such questions correctly."""
    covered = {(m.aggregation, (m.sql_expression or "").strip()) for m in metrics}
    join_columns = _join_columns(ctx)
    shared = _shared_columns(ctx)
    candidates = []
    for table, columns in ctx.tables.items():
        for col in columns:
            name = col["name"]
            lowered = _snake(name)
            if (not _is_numeric(col) or _ID_LIKE.search(lowered) or _NEVER_MEASURE.search(lowered)
                    or _CODE_COLUMN.search(lowered) or (table, name) in join_columns):
                continue
            distinct = ctx.distinct_counts.get(table, {}).get(name, -1)
            if 0 <= distinct < 2:
                continue  # one value throughout (Northwind's invoice amounts are all 0): nothing to measure
            signal = bool(_MEASURE_WORDS.search(lowered)) and not _NOT_SUMMABLE.search(lowered)
            summable = (signal or distinct > _SUMMABLE_MIN_DISTINCT) and not _NOT_SUMMABLE.search(lowered) \
                and (not 0 <= distinct <= _SUMMABLE_MIN_DISTINCT or bool(_FLOW.search(lowered)))
            if _PRICE_COLUMN.search(lowered) and any(_QUANTITY_COLUMN.search(c["name"].lower()) for c in columns):
                summable = False  # a unit price beside a quantity: the line amount is the sum that means something
            aggregations = ["SUM", "AVG"] if summable else ["AVG"]
            candidates.append((0 if signal else 1, table, name, aggregations))
    added: list[SemanticMetric] = []
    for _, table, column, aggregations in sorted(candidates):
        for aggregation in aggregations:
            if len(metrics) + len(added) >= MAX_METRICS:
                return added
            expression = f"{table}.{column}"
            if (aggregation, expression) in covered:
                continue
            subject = _snake(column)
            if len(ctx.tables) > 1 and (subject in shared or subject in _GENERIC_MEASURE):
                subject = _snake(f"{table}_{column}")  # average_invoice_total, not average_total
            name = f"{'total' if aggregation == 'SUM' else 'average'}_{subject}"
            label = f"{'Total' if aggregation == 'SUM' else 'Average'} {_title(subject)}"
            if _AVERAGE_WORD.search(_snake(column)):
                # The column is already an average ("average_cost_for_two"): its AVG keeps the
                # column's name, and its SUM says plainly that it adds averages up.
                name = _snake(column) if aggregation == "AVG" else f"total_{_snake(column)}"
                label = _title(column) if aggregation == "AVG" else f"Sum of {_title(column)}"
            if name in seen:
                continue
            seen.add(name)
            added.append(SemanticMetric(name=name, display_name=label, description=f"{label} (added for coverage)",
                                        aggregation=aggregation, sql_expression=expression, required_tables=[table]))
            notes.append(f"added metric {name}")
    return added


def _coverage_dimensions(ctx: SchemaContext, dimensions: list[SemanticDimension], seen: set[str],
                         notes: list[str]) -> list[SemanticDimension]:
    """Dimensions for the categorical columns the LLM left out, up to MAX_DIMENSIONS in total.

    On a restaurant file the 4B model proposed 5 dimensions and left out price range, table booking
    and the restaurant name: "average rating by price range" was then answered by city, and "top 10
    restaurants by votes" by city. Added: strings with 2-500 distinct values (flags, labels,
    categories), integers with 2-20 distinct values (levels, small codes), and the entity's name
    column even when its cardinality is high (for "top N <entity>"). Free text, coordinates and
    columns with one value are left out."""
    covered = {d.sql_expression.strip() for d in dimensions}
    shared = _shared_columns(ctx)
    keys = _join_columns(ctx)
    candidates = []
    for table, columns in ctx.tables.items():
        rows = ctx.row_counts.get(table, 0)
        names = {c["name"].lower(): c["name"] for c in columns}
        first, last = names.get("first_name") or names.get("firstname"), names.get("last_name") or names.get("lastname")
        person = bool(first and last)
        if person:
            # One name per person ("revenue by sales support agent"), not first and last names apart.
            expression = f"{table}.{first} || ' ' || {table}.{last}"
            distinct = ctx.distinct_counts.get(table, {}).get(last, -1)
            if expression not in covered and distinct >= 2:
                # Ranked with the labels: last, it lost its slot on Northwind ("sales by employee").
                candidates.append((0, distinct, table, None, expression))
        for col in columns:
            name, lowered = col["name"], col["name"].lower()
            expression = f"{table}.{name}"
            distinct = ctx.distinct_counts.get(table, {}).get(name, -1)
            if (expression in covered or distinct < 2 or _NEVER_MEASURE.search(lowered) or _is_temporal(col)
                    or _CONTACT.search(lowered) or _NOT_A_BREAKDOWN.search(lowered)
                    or (person and name in (first, last)) or (table, name) in keys):
                continue
            if _is_numeric(col):
                # A small code is a breakdown (the taxi file's VendorID: 3 values, RatecodeID: 7);
                # join keys are left out above, and a large ID is no breakdown.
                if distinct > _NUMERIC_DIMENSION_MAX_DISTINCT:
                    continue
                rank = 1
            elif distinct <= _DIMENSION_MAX_DISTINCT and not (_FREE_TEXT.search(lowered) and distinct > _FREE_TEXT_MIN_DISTINCT):
                rank = 0  # "rating_text" with 6 values is a label; "address" with 300 is not
            elif _ENTITY_NAME.search(lowered) and not _FREE_TEXT.search(lowered):
                # The entity's name, repeated (chains, branches) or unique (TPC-H's c_name: "top 5
                # customers by revenue" had nothing to group by when unique names were left out).
                rank = 2
            else:
                continue
            candidates.append((rank, distinct, table, name, expression))
    added: list[SemanticDimension] = []
    # Within a rank, names first (a person's full name, product_name, company): on Northwind 30 slots
    # went to small lookup labels ahead of product_name (45 values), and "which products sold the
    # most" had no product to group by.
    def named(candidate) -> bool:
        return candidate[3] is None or bool(_ENTITY_NAME.search(candidate[3].lower())) or candidate[3].lower() == "company"

    for rank, distinct, table, column, expression in sorted(
            candidates, key=lambda c: (c[0], not named(c)) + c[1:3] + (c[3] or "",)):
        if len(dimensions) + len(added) >= _dimension_cap(ctx):
            break
        if column is None:  # a person's full name
            name, label = _snake(f"{table}_name"), f"{_title(table)} Name"
        elif len(ctx.tables) > 1 and (column.lower() in _GENERIC_COLUMN or _snake(column) in shared):
            name, label = _snake(f"{table}_{column}"), _title(f"{table}_{column}")
        else:
            name, label = _snake(column), _title(column)
        if name in seen:  # the same column name in another table: the validator drops it if it cannot join
            name, label = _snake(f"{table}_{column or 'name'}"), _title(f"{table}_{column or 'name'}")
            if name in seen:
                continue
        seen.add(name)
        cardinality = "low" if distinct < 20 else "medium" if distinct < _DIMENSION_MAX_DISTINCT else "high"
        added.append(SemanticDimension(name=name, display_name=label, sql_expression=expression,
                                       required_tables=[table], cardinality=cardinality))
        notes.append(f"added dimension {name}")
    return added


_ROLE_KEY = re.compile(r"^(?P<role>[a-z][a-z0-9_]*?)_(id|key|ref|no|number)$")
MAX_ROLE_DIMENSIONS = 3


def _role_dimensions(ctx: SchemaContext, dimensions: list[SemanticDimension], seen: set[str],
                     notes: list[str]) -> list[SemanticDimension]:
    """The name of the row a foreign key points at, under the key's role: customer.support_rep_id ->
    employee becomes "Support Rep Name" (the employee's name). The data model only said "Employee
    Name", and "revenue by sales support agent" was refused. Keys named after their table
    (customer_id -> customer) add nothing."""
    names_by_table: dict[str, SemanticDimension] = {}
    for dimension in dimensions:
        if len(dimension.required_tables) == 1 and dimension.name.endswith("_name"):
            names_by_table.setdefault(dimension.required_tables[0], dimension)
    added: list[SemanticDimension] = []
    for join in ctx.join_paths:
        match = _JOIN_COLUMN.match(str(join.get("on", "")))
        if not match or join.get("from") == join.get("to") or len(added) >= MAX_ROLE_DIMENSIONS:
            continue
        key = _ROLE_KEY.match(_snake(match.group(2)))
        target = names_by_table.get(join.get("to"))
        if key is None or target is None:
            continue
        role, table = key.group("role"), _snake(join["to"])
        if role in (table, re.sub(r"s$", "", table)) or table.endswith(role):
            continue
        name = f"{role}_name"
        if name in seen:
            continue
        seen.add(name)
        label = f"{_title(role)} Name"
        added.append(target.model_copy(update={"name": name, "display_name": label}))
        notes.append(f"added dimension {name} ({target.name} via {match.group(1)}.{match.group(2)})")
    return added


def _key_dimension_reason(ctx: SchemaContext, expression: str) -> str | None:
    """Why a proposed dimension on a bare key column is no breakdown, or None to keep it. gpt-4.1-mini
    proposed payment.customer_id on Pagila, and "which 5 customers paid the most" was answered with
    customer numbers. A key pointing at a table with a name gives way to that name; a table's own row
    ID with hundreds of values is dropped. store_id (stores have no name) stays."""
    match = _JOIN_COLUMN.fullmatch(expression.strip())
    if match is None:
        return None
    table, column = match.group(1), match.group(2)
    partners = [t for j in list(ctx.join_paths) + list(ctx.inferred_joins)
                for pairs in [_JOIN_COLUMN.findall(str(j.get("on", "")))] if (table, column) in pairs
                for t, _ in pairs if t != table]
    own_id = _snake(column) in ("id", f"{_snake(table)}_id", f"{re.sub(r's$', '', _snake(table))}_id")
    if not own_id and (table, column) not in _join_columns(ctx):
        return None
    for partner in dict.fromkeys(partners + ([table] if own_id else [])):
        names = {c["name"].lower() for c in ctx.tables.get(partner, [])}
        if any(_ENTITY_NAME.search(n) for n in names) or {"first_name", "last_name"} <= names:
            return f"a key of {partner}, which has a name"
    if own_id and ctx.distinct_counts.get(table, {}).get(column, -1) >= _DIMENSION_MAX_DISTINCT:
        return "a row identifier"
    return None


def _measured_cardinality(ctx: SchemaContext, expression: str) -> str | None:
    """The cardinality of a plain column from its distinct count; None for an expression. The LLM's
    own guess called 7,446 restaurant names "medium", so the value index embedded 5,000 of them and
    took 45 s to build instead of a few."""
    match = _BARE_COLUMN.match(expression)
    if match is None:
        return None
    table = expression.split(".", 1)[0].strip()
    distinct = ctx.distinct_counts.get(table, {}).get(match.group(1), -1)
    if distinct < 0:
        return None
    return "low" if distinct < 20 else "medium" if distinct < _DIMENSION_MAX_DISTINCT else "high"


def _is_numeric(col: dict) -> bool:
    return any(t in col.get("type", "").upper() for t in _NUMERIC)


def _is_temporal(col: dict) -> bool:
    return any(t in col.get("type", "").upper() for t in ("DATE", "TIMESTAMP", "TIME"))


MAX_FILTER_COLUMNS = 24
_UNITS = ((re.compile(r"(^|_)(milliseconds|millis|ms)$"), "milliseconds"),
          (re.compile(r"(^|_)(seconds|secs?)$"), "seconds"), (re.compile(r"(^|_)(minutes|mins?)$"), "minutes"),
          (re.compile(r"(^|_)(hours|hrs?)$"), "hours"), (re.compile(r"(^|_)days$"), "days"),
          (re.compile(r"(^|_)bytes$"), "bytes"), (re.compile(r"(^|_)(percent|pct)$"), "percent"))
_DURATION_UNITS = {"milliseconds", "seconds", "minutes", "hours", "days"}


def _filter_columns(ctx: SchemaContext, metric_tables: list[str], time_expressions: set[str], seen: set[str],
                    notes: list[str]) -> list[SemanticFilterColumn]:
    """Numbers and dates of the tables the metrics count or sum, as conditions: "How many tracks are
    longer than 5 minutes?" (track.milliseconds), "How many rentals have not been returned?"
    (rental.return_date IS NULL), "companies with negative earnings per share" (S&P file). Before,
    these were refused or answered without the condition: only breakdown columns could be filtered.
    Keys, coordinates, audit stamps and dates that are already time dimensions are left out."""
    columns: list[SemanticFilterColumn] = []
    # A column a join uses is a key, whatever its name (Chinook: employee.reports_to).
    join_columns = {(m.group(1), m.group(2)) for j in [*ctx.join_paths, *ctx.inferred_joins]
                    for m in _JOIN_COLUMN.finditer(j.get("on", ""))}
    for table in dict.fromkeys(metric_tables):
        for col in ctx.tables.get(table, []):
            raw, snake = col["name"], _snake(col["name"])
            if f"{table}.{raw}" in time_expressions or (table, raw) in join_columns:
                continue
            col_type = col.get("type", "").upper()
            if _is_numeric(col) and not _ID_LIKE.search(snake) and not _NEVER_MEASURE.search(snake):
                kind = "number"
            elif _is_temporal(col) and ("DATE" in col_type or "STAMP" in col_type) and not _AUDIT_TIME.search(snake):
                kind = "date"
            else:
                continue
            name = snake if snake not in seen else _snake(f"{table}_{raw}")
            if name in seen or len(columns) >= MAX_FILTER_COLUMNS:
                continue
            unit = next((u for pattern, u in _UNITS if pattern.search(snake)), None) if kind == "number" else None
            label = _title(raw) if name == snake else _title(f"{table}_{raw}")
            description = f"{label} of each {table.replace('_', ' ')} row"
            if unit:
                description += f", in {unit}" + (" (its length or duration)" if unit in _DURATION_UNITS else "")
            if kind == "date":
                description += "; empty when it has not happened yet"
            seen.add(name)
            columns.append(SemanticFilterColumn(name=name, display_name=label, sql_expression=f"{table}.{raw}",
                                                required_tables=[table], kind=kind, unit=unit, description=description))
    if columns:
        notes.append(f"added {len(columns)} filter columns: {', '.join(c.name for c in columns)}")
    return columns


_BARE_COLUMN = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _mislabeled(name: str, table: str, column: str) -> bool:
    """A dimension whose name shares no word with its column: "product_category" on products.list_price,
    "payment_type" on invoices.due_date. The table's words name nothing (product_status on
    products.discontinued stays), and a generic column means its table (film_category on category.name,
    shipper on shippers.company). Words of 3+ letters, plurals folded."""
    def words(text: str) -> set[str]:
        return {re.sub(r"ies$", "y", re.sub(r"(?<=[^su])s$", "", w)) for w in _snake(text).split("_") if len(w) > 2}

    table_words = words(table)
    generic = column.lower() in _GENERIC_COLUMN or column.lower() == "company"
    column_words = words(column) | (table_words if generic else set())
    rest = words(name) - table_words - _MISLABEL_EXEMPT
    return bool(rest) and not words(name) & column_words


# Names that may stand for any column: a dimension called "status" over "discontinued", "type" over "kind".
_MISLABEL_EXEMPT = {"status", "type", "kind", "flag", "group", "segment", "label", "class", "level"}


def _single_valued(ctx: SchemaContext, aggregation: str, expression: str | None) -> bool:
    """A SUM or AVG over a column with one value throughout: Northwind's "Total Revenue" was
    SUM(invoices.amount_due), and every amount due in the sample is 0, so all sales read 0."""
    match = _JOIN_COLUMN.fullmatch((expression or "").strip()) if aggregation in ("SUM", "AVG") else None
    return bool(match) and 0 <= ctx.distinct_counts.get(match.group(1), {}).get(match.group(2), -1) < 2


def _meaningless_sum(aggregation: str, expression: str | None) -> bool:
    """SUM over a bare score, level or coordinate column ("total rating", "total latitude")."""
    match = _BARE_COLUMN.match(expression or "") if aggregation == "SUM" else None
    return bool(match) and bool(_NOT_SUMMABLE.search(match.group(1).lower())
                                or _NEVER_MEASURE.search(match.group(1).lower()))


def _key_measure(ctx: SchemaContext, aggregation: str, expression: str | None) -> bool:
    """SUM or AVG over an identifier, a join key or a numeric code."""
    match = _JOIN_COLUMN.fullmatch((expression or "").strip()) if aggregation in ("SUM", "AVG") else None
    if not match:
        return False
    table, column = match.group(1), match.group(2)
    snake = _snake(column)
    return bool(_ID_LIKE.search(snake) or _CODE_COLUMN.search(snake) or (table, column) in _join_columns(ctx))


def _entity_id(ctx: SchemaContext, expression: str | None) -> tuple[str, str] | None:
    """The ID column to count instead of a name column (restaurant_name -> restaurant_id), and the
    entity's plural ("restaurants"). Names repeat: a chain's branches share one, and COUNT(DISTINCT
    restaurant_name) answered "how many restaurants offer table booking?" with 1,027 for 1,158."""
    match = _BARE_COLUMN.match(expression or "")
    if not match or not _ENTITY_NAME.search(match.group(1).lower()):
        return None
    table, column = expression.strip().rsplit(".", 1)
    stem = re.sub(r"_?(name|title)$", "", _snake(column))
    by_snake = {_snake(c["name"]): c["name"] for c in ctx.tables.get(table, [])}
    distinct = ctx.distinct_counts.get(table, {})
    for candidate in (f"{stem}_id", f"{stem}id", "id"):
        actual = by_snake.get(candidate)
        if actual and distinct.get(actual, -1) >= distinct.get(column, -1) and _BARE_COLUMN.match(f"{table}.{actual}"):
            return f"{table}.{actual}", f"{stem}s" if stem else "items"
    return None


_AVERAGE_WORD = re.compile(r"(^|_)(average|avg|mean)(_|$)")
_TOTAL_WORD = re.compile(r"^(total|sum)_(?=[a-z])")
_COUNT_WORD = re.compile(r"(^|_)(count|number|num)(_|$)")


def _honest_name(metric: ProposedMetric, notes: list[str]) -> ProposedMetric:
    """A sum or count must not be called an average. The restaurant file's column is named
    "average_cost_for_two"; the LLM proposed SUM over it under that name, and "average cost for
    two" answered 11,453,662 (the sum over 9,551 restaurants). Renamed "total_<name>", labelled
    "Sum of <label>"; the coverage metrics add the real average."""
    snake = _snake(metric.name)
    total = _TOTAL_WORD.match(snake)
    if metric.aggregation in ("COUNT", "COUNT_DISTINCT") and total and not _COUNT_WORD.search(snake):
        # Pagila: COUNT(payment_id) named "total_payments" beside SUM(amount) "total_revenue";
        # "which customers paid the most" took the count (46 payments, not $221).
        rest = snake[total.end():]
        name, label = f"{rest}_count", f"Number of {_title(rest)}"
        notes.append(f"renamed metric {metric.name} -> {name}: a count is not a total")
        return metric.model_copy(update={"name": name, "display_name": label, "description": label})
    if metric.aggregation not in ("SUM", "COUNT", "COUNT_DISTINCT") or not _AVERAGE_WORD.search(snake):
        return metric
    prefix = "total" if metric.aggregation == "SUM" else "count"
    already = re.match(r"^(total|sum|count|number)_", snake)
    name = snake if already else f"{prefix}_{snake}"  # "total_average_cost" stays; only its label changes
    base = metric.display_name or _title(metric.name)
    base = re.sub(r"^(total|sum|count|number)( of)? ", "", base, flags=re.IGNORECASE)
    label = f"{'Sum' if metric.aggregation == 'SUM' else 'Count'} of {base}"
    notes.append(f"renamed metric {metric.name} -> {name}: a {metric.aggregation} is not an average")
    return metric.model_copy(update={"name": name, "display_name": label, "description": label})


def _snake(name: str) -> str:
    # camelCase and acronyms split first: VendorID -> vendor_id, PULocationID -> pu_location_id
    # ("locationid" was not recognised as an ID, and the taxi upload got "Total Locationid").
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name.strip())
    return re.sub(r"[^a-z0-9]+", "_", split.lower()).strip("_") or "item"


def _title(name: str) -> str:
    return name.replace("_", " ").title()


def complete_schema(proposal: SchemaProposal, ctx: SchemaContext) -> tuple[SemanticSchema, list[str]]:
    """The full schema, plus notes on what was repaired or dropped (for logs and the job status)."""
    resolver = _Resolver(ctx)
    notes: list[str] = []
    seen: set[str] = set()

    def unique(name: str) -> str | None:
        name = _snake(name)
        if name in seen:
            notes.append(f"dropped duplicate name {name}")
            return None
        seen.add(name)
        return name

    def resolved(kind: str, name: str, expression: str | None):
        if not expression:
            return None
        result = resolver.resolve(expression)
        if result is None:
            notes.append(f"dropped {kind} {name}: cannot resolve columns in {expression!r}")
        elif result[0] != expression:
            notes.append(f"repaired {kind} {name}: {expression!r} -> {result[0]!r}")
        return result

    metrics: list[SemanticMetric] = []
    column_names = {_snake(c["name"]) for cols in ctx.tables.values() for c in cols}
    for m in proposal.metrics:
        m = _honest_name(m, notes)
        if _snake(m.name) in column_names:
            # A metric named after a column ("city" = COUNT(DISTINCT city)) took the name the
            # column's dimension needs; that dimension became "zomato_city".
            prefix = {"COUNT_DISTINCT": "unique", "COUNT": "count", "SUM": "total", "AVG": "average"}.get(
                m.aggregation, "metric")
            notes.append(f"renamed metric {m.name} -> {prefix}_{_snake(m.name)}: a column has that name")
            m = m.model_copy(update={"name": f"{prefix}_{_snake(m.name)}"})
        name = unique(m.name)
        if name is None:
            continue
        fields: dict = {}
        tables: list[str] = []
        parts = (("numerator_sql", m.numerator_sql), ("denominator_sql", m.denominator_sql)) \
            if m.aggregation == "RATIO" else (("sql_expression", m.sql_expression),)
        ok = True
        for field, expression in parts:
            result = resolved("metric", name, expression)
            if result is None:
                ok = False
                break
            fields[field] = result[0]
            tables += [t for t in result[1] if t not in tables]
        if not ok:
            continue
        if not tables:
            notes.append(f"dropped metric {name}: references no column")
            continue
        entity = _entity_id(ctx, fields.get("sql_expression")) if m.aggregation == "COUNT_DISTINCT" else None
        if entity:
            notes.append(f"metric {name} counts {entity[0]} instead of {fields['sql_expression']}: names repeat")
            fields["sql_expression"] = entity[0]
            if entity[1] not in seen and entity[1] not in column_names:
                seen.discard(name)
                name = unique(entity[1])
                m = m.model_copy(update={"display_name": _title(name), "description": f"Number of {_title(name).lower()}"})
        if _single_valued(ctx, m.aggregation, fields.get("sql_expression")):
            notes.append(f"dropped metric {name}: {fields['sql_expression']} has one value throughout")
            seen.discard(name)
            continue
        if _meaningless_sum(m.aggregation, fields.get("sql_expression")):
            notes.append(f"dropped metric {name}: a sum of {fields['sql_expression']} means nothing")
            seen.discard(name)
            continue
        if _key_measure(ctx, m.aggregation, fields.get("sql_expression")):
            # The employees upload had "Total Current Dept Emp Emp No": a sum of employee numbers.
            notes.append(f"dropped metric {name}: {fields['sql_expression']} is a key or a code, not a measure")
            seen.discard(name)
            continue
        base_filter = None
        if m.base_filter:
            result = resolved("metric filter", name, m.base_filter)
            if result is None:
                # Without its condition the metric would count more than its name says ("paid
                # revenue" summing every payment): dropped, never widened (review B9).
                notes.append(f"dropped metric {name}: its condition {m.base_filter!r} does not resolve")
                seen.discard(name)
                continue
            base_filter = result[0]
            tables += [t for t in result[1] if t not in tables]
        try:
            metrics.append(SemanticMetric(
                name=name, display_name=m.display_name or _title(name), description=m.description or _title(name),
                aggregation=m.aggregation, required_tables=tables, base_filter=base_filter, **fields,
            ))
        except ValueError as exc:
            notes.append(f"dropped metric {name}: {str(exc)[:120]}")

    dimensions: list[SemanticDimension] = []
    dated: list[tuple[str, str, str, str]] = []  # dimensions that are dates: (name, label, column, table)
    for d in proposal.dimensions:
        name = unique(d.name)
        result = resolved("dimension", d.name, d.sql_expression) if name else None
        if result is None or not result[1]:
            continue
        match = _JOIN_COLUMN.fullmatch(result[0].strip())
        if match and _mislabeled(name, match.group(1), match.group(2)):
            # Northwind: product_category = products.list_price, payment_type = invoices.due_date.
            notes.append(f"dropped dimension {name}: its name says nothing of {result[0]}")
            seen.discard(name)
            continue
        if match and _NOT_A_BREAKDOWN.search(match.group(2).lower()):
            notes.append(f"dropped dimension {name}: {result[0]} is per-row text or contact detail")
            seen.discard(name)
            continue
        if (reason := _key_dimension_reason(ctx, result[0])) is not None:
            notes.append(f"dropped dimension {name}: {result[0]} is {reason}")
            seen.discard(name)
            continue
        column = next((c for c in ctx.tables.get(match.group(1), []) if c["name"] == match.group(2)), None) if match else None
        if column is not None and _is_temporal(column):
            # A date is a time dimension, grouped by day..year. As a dimension it grouped by single
            # days ("hired each year" came back per hire_date) and was rejected as a time dimension.
            notes.append(f"dimension {name}: {result[0]} is a date; a time dimension instead")
            dated.append((name, d.display_name or _title(name), result[0], result[1][0]))
            continue
        dimensions.append(SemanticDimension(name=name, display_name=d.display_name or _title(name),
                                            sql_expression=result[0], required_tables=result[1],
                                            cardinality=_measured_cardinality(ctx, result[0]) or d.cardinality))

    time_dimensions: list[SemanticTimeDimension] = []
    for t in proposal.time_dimensions:
        name = unique(t.name)
        result = resolved("time dimension", t.name, t.sql_expression) if name else None
        if result is None:
            continue
        if len(result[1]) != 1:
            notes.append(f"dropped time dimension {name}: spans tables {result[1]}")
            continue
        time_dimensions.append(SemanticTimeDimension(name=name, display_name=t.display_name or _title(name),
                                                     sql_expression=result[0], table=result[1][0],
                                                     default_granularity=t.default_granularity))
    timed_columns = {t.sql_expression for t in time_dimensions}
    for name, label, expression, table in dated:
        if expression in timed_columns or len(time_dimensions) >= MAX_TIME_DIMENSIONS:
            seen.discard(name)
            continue
        timed_columns.add(expression)
        time_dimensions.append(SemanticTimeDimension(name=name, display_name=label, sql_expression=expression,
                                                     table=table, default_granularity="month"))

    metrics += _entity_count_metrics(ctx, metrics, seen, notes)
    metrics += _line_amount_metrics(ctx, metrics, seen, notes)
    metrics += _measure_metrics(ctx, metrics, seen, notes)
    dimensions += _coverage_dimensions(ctx, dimensions, seen, notes)
    dimensions += _role_dimensions(ctx, dimensions, seen, notes)
    # The LLM left out the date in 2 of 3 runs on the retail upload ("revenue by month" had nothing
    # to group by), and on Pagila proposed payment_date only ("rentals each month" had no rental
    # date). Each table a metric counts or sums gets its first date column (not an audit stamp).
    timed = {t.table for t in time_dimensions}
    metric_tables = [t for m in metrics for t in m.required_tables[:1]]
    for table in dict.fromkeys(metric_tables + ([] if time_dimensions else list(ctx.tables))):
        if table in timed or len(time_dimensions) >= MAX_TIME_DIMENSIONS:
            continue
        col = next((c for c in ctx.tables.get(table, []) if _is_temporal(c) and not _AUDIT_TIME.search(c["name"].lower())),
                   None)
        if col is None:
            continue
        name = _snake(col["name"]) if _snake(col["name"]) not in seen else _snake(f"{table}_{col['name']}")
        if name in seen:
            continue
        seen.add(name)
        timed.add(table)
        time_dimensions.append(SemanticTimeDimension(
            name=name, display_name=_title(col["name"]), sql_expression=f"{table}.{col['name']}",
            table=table, default_granularity="month"))
        notes.append(f"added time dimension {name}")

    filter_columns = _filter_columns(ctx, metric_tables, {t.sql_expression for t in time_dimensions}, seen, notes)

    joins = [SemanticJoin.model_validate({"from": j["from"], "to": j["to"], "on": j["on"],
                                          "type": j.get("type", "LEFT"), "relationship": j.get("relationship")})
             for j in list(ctx.join_paths) + list(ctx.inferred_joins)]
    # No name from the LLM: the (first) table name says more than "dataset".
    dataset = _snake(proposal.dataset or next(iter(ctx.tables), "dataset"))
    schema = SemanticSchema(
        version="1.0", dataset=dataset, description=proposal.description or _title(dataset),
        tables=[SemanticTable(name=table, link=table in _link_tables(ctx)) for table in ctx.tables], joins=joins,
        metrics=metrics, dimensions=dimensions, time_dimensions=time_dimensions, filter_columns=filter_columns,
    )
    for note in notes:
        logger.info("Schema completion: %s", note)
    return schema, notes
