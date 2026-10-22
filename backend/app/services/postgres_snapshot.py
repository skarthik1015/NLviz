"""Copy a Postgres database into Parquet when Jammi cannot query it live.

Jammi registers a Postgres database as one source, and one column of an unsupported type fails
the whole source (domains, jsonb, citext, time with time zone: issue J33), while money values come
back 100 times too large (J34). Such a database is copied with NLviz's own driver instead, like a
MySQL snapshot: each table (partitioned tables whole, never their partitions) becomes one Parquet
file, with the awkward types cast to something Arrow holds (money -> numeric, jsonb/citext/
tsvector/inet/arrays/intervals -> text, a domain -> its base type). Binary columns are left out.
"""
from __future__ import annotations

import datetime as dt
import decimal
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.services.mysql_snapshot import SnapshotTooLarge, TableSnapshot, check_row_limit, snapshot_table_name
from app.services.jammi_uploads import snake_identifier

logger = logging.getLogger(__name__)

_BATCH_ROWS = 10_000
# udt names that Jammi cannot register (J33) or reads wrongly (J34); any domain as well.
_LIVE_BLOCKERS = {"jsonb": "jsonb", "citext": "citext", "timetz": "time with time zone", "money": "money"}
_INTEGERS = {"smallint", "integer", "bigint"}
_FLOATS = {"real", "double precision"}
_TEXT = {"text", "character varying", "character", '"char"', "name"}


def live_blockers(conn, schema: str, tables: list[str]) -> list[str]:
    """Columns that stop a live Jammi source from working ("film.release_year (domain year)")."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, udt_name, domain_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = ANY(%s) ORDER BY table_name, ordinal_position",
            (schema, list(tables)),
        )
        found = []
        for table, column, udt, domain in cur.fetchall():
            if domain:
                found.append(f"{table}.{column} (domain {domain})")
            elif udt in _LIVE_BLOCKERS:
                found.append(f"{table}.{column} ({_LIVE_BLOCKERS[udt]})")
        return found


def pre_epoch_timestamps(conn, schema: str, tables: list[str]) -> list[str]:
    """`timestamp` (without time zone) columns holding a value before 1970: every live query that
    returns such a column fails in Jammi (J36; Chinook's employee birth dates). timestamptz and date
    are read correctly. Each probe is bounded; one that times out counts as clean."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = ANY(%s) AND udt_name = 'timestamp' "
            "ORDER BY table_name, ordinal_position",
            (schema, list(tables)),
        )
        columns = cur.fetchall()
        cur.execute("SET LOCAL statement_timeout = '5s'")
        found = []
        for table, column in columns:
            ref = '"' + schema.replace('"', '""') + '"."' + table.replace('"', '""') + '"'
            col = '"' + column.replace('"', '""') + '"'
            try:
                cur.execute(f"SELECT 1 FROM {ref} WHERE {col} < '1970-01-01' LIMIT 1")
                if cur.fetchone():
                    found.append(f"{table}.{column}")
            except Exception:
                conn.rollback()
                cur.execute("SET LOCAL statement_timeout = '5s'")
        return found


def _plan(data_type: str, udt: str, precision: int | None, scale: int | None, quoted: str
          ) -> tuple[str, pa.DataType] | None:
    """(select expression, Arrow type) for one column; None leaves it out (binary)."""
    if data_type == "bytea":
        return None
    if udt == "money":
        return f"{quoted}::numeric", pa.decimal128(19, 2)
    if data_type in _INTEGERS:
        return quoted, pa.int64()
    if data_type == "numeric":
        if precision and precision <= 38:
            return quoted, pa.decimal128(precision, scale or 0)
        return f"{quoted}::double precision", pa.float64()  # unconstrained numeric
    if data_type in _FLOATS:
        return quoted, pa.float64()
    if data_type == "boolean":
        return quoted, pa.bool_()
    if data_type == "date":
        return quoted, pa.date32()
    if data_type == "timestamp without time zone":
        return quoted, pa.timestamp("us")
    if data_type == "timestamp with time zone":
        return quoted, pa.timestamp("us", tz="UTC")
    if data_type in _TEXT:
        return quoted, pa.string()
    if data_type == "ARRAY":
        # A plain list ("Deleted Scenes, Trailers") that list-column detection can split into items,
        # not Postgres's literal ('{"Deleted Scenes",Trailers}').
        return f"array_to_string({quoted}, ', ')", pa.string()
    return f"{quoted}::text", pa.string()  # enum, json(b), citext, tsvector, inet, uuid, arrays, intervals, times


def _convert(arrow_type: pa.DataType) -> Callable[[Any], Any]:
    if pa.types.is_decimal(arrow_type):
        return lambda v: v if v is None or isinstance(v, decimal.Decimal) else decimal.Decimal(str(v))
    if pa.types.is_timestamp(arrow_type) and arrow_type.tz:
        return lambda v: v.astimezone(dt.timezone.utc) if isinstance(v, dt.datetime) and v.tzinfo else v
    return lambda v: v


def snapshot_postgres_tables(connect: Callable[[], Any], schema: str, tables: list[str], workdir: Path, *,
                             max_rows: int) -> list[TableSnapshot]:
    """Copy each table into `<workdir>/<logical>.parquet` (snake_case names, as MySQL snapshots).
    Stops with SnapshotTooLarge once the total passes max_rows."""
    snapshots: list[TableSnapshot] = []
    total = 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, data_type, udt_name, numeric_precision, numeric_scale "
                "FROM information_schema.columns WHERE table_schema = %s AND table_name = ANY(%s) "
                "ORDER BY table_name, ordinal_position",
                (schema, list(tables)),
            )
            columns: dict[str, list[tuple]] = {}
            for row in cur.fetchall():
                columns.setdefault(row[0], []).append(row[1:])
            check_row_limit(cur, ['"' + schema.replace('"', '""') + '"."' + t.replace('"', '""') + '"' for t in tables],
                            max_rows)
        for table in tables:
            selects, fields, renamed, names = [], [], {}, set()
            for name, data_type, udt, precision, scale in columns.get(table, []):
                quoted = '"' + name.replace('"', '""') + '"'
                plan = _plan(data_type, udt, precision, scale, quoted)
                if plan is None:
                    continue
                logical = snake_identifier(name, fallback=f"column_{len(fields) + 1}", digit_prefix="c_")
                base, n = logical, 2
                while logical in names:
                    logical, n = f"{base}_{n}", n + 1
                names.add(logical)
                if logical != name:
                    renamed[name] = logical
                selects.append(plan[0])
                fields.append(pa.field(logical, plan[1]))
            if not fields:
                continue
            schema_ = pa.schema(fields)
            converters = [_convert(f.type) for f in schema_]
            logical_table = snapshot_table_name(table)
            path = workdir / f"{logical_table}.parquet"
            rows_written = 0
            quoted_table = '"' + schema.replace('"', '""') + '"."' + table.replace('"', '""') + '"'
            with conn.cursor(name=f"nlviz_snapshot_{len(snapshots)}") as cur, \
                    pq.ParquetWriter(path, schema_, compression="zstd") as writer:
                cur.itersize = _BATCH_ROWS
                cur.execute(f"SELECT {', '.join(selects)} FROM {quoted_table}")
                while True:
                    rows = cur.fetchmany(_BATCH_ROWS)
                    if not rows:
                        break
                    total += len(rows)
                    if total > max_rows:
                        raise SnapshotTooLarge(f"The database has more than {max_rows:,} rows, the snapshot limit")
                    arrays = [pa.array([convert(row[i]) for row in rows], type=field_.type)
                              for i, (convert, field_) in enumerate(zip(converters, schema_))]
                    writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema_))
                    rows_written += len(rows)
            snapshots.append(TableSnapshot(table=logical_table, path=path, num_rows=rows_written,
                                           renamed_columns=renamed))
    finally:
        conn.close()
    return snapshots
