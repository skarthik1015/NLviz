"""Snapshot a MySQL database into Parquet for Jammi.

Jammi can federate MySQL live, but its pushed-down SQL changes meaning in MySQL (issue J27: date
subtraction, `||`, NULL ordering), which gives silently wrong answers. So by default NLviz copies the
tables with its own driver and registers them as Parquet, which Jammi evaluates with DataFusion's
semantics. The snapshot is taken when the connection is created; refresh is a later phase.
"""
from __future__ import annotations

import datetime as dt
import decimal
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

from app.services.jammi_uploads import UploadError, snake_identifier

logger = logging.getLogger(__name__)

_BATCH_ROWS = 50_000
_INTEGER_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year"}
_FLOAT_TYPES = {"float", "double", "real"}
_TEXT_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set", "json"}
_BINARY_TYPES = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}


@dataclass(frozen=True)
class MySQLColumn:
    name: str
    data_type: str  # information_schema.columns.data_type, lowercase (e.g. "decimal")
    column_type: str  # full type (e.g. "bigint unsigned", "decimal(10,2)")
    precision: int | None = None
    scale: int | None = None


def arrow_type_for(column: MySQLColumn) -> pa.DataType:
    kind = column.data_type.lower()
    if kind in _INTEGER_TYPES:
        if kind == "bigint" and "unsigned" in column.column_type.lower():
            return pa.uint64()
        return pa.int64()
    if kind in ("decimal", "numeric"):
        precision = column.precision or 38
        if precision <= 38:
            return pa.decimal128(precision, column.scale or 0)
        return pa.float64()
    if kind in _FLOAT_TYPES:
        return pa.float64()
    if kind == "bit":
        return pa.int64()
    if kind == "date":
        return pa.date32()
    if kind in ("datetime", "timestamp"):
        return pa.timestamp("us")
    if kind in _BINARY_TYPES:
        return pa.binary()
    return pa.string()  # text types, time, json, geometry (as text), anything unknown


def _converter(arrow_type: pa.DataType) -> Callable[[Any], Any]:
    """Map PyMySQL values onto what pyarrow accepts for the column's type."""
    if pa.types.is_timestamp(arrow_type):
        # Zero dates ('0000-00-00 00:00:00') come back as strings: treat them as missing.
        return lambda v: v if v is None or isinstance(v, dt.datetime) else None
    if pa.types.is_date32(arrow_type):
        return lambda v: v if v is None or isinstance(v, dt.date) else None
    if pa.types.is_decimal(arrow_type):
        return lambda v: v if v is None or isinstance(v, decimal.Decimal) else decimal.Decimal(str(v))
    if pa.types.is_int64(arrow_type) or pa.types.is_uint64(arrow_type):
        return lambda v: int.from_bytes(v, "big") if isinstance(v, (bytes, bytearray)) else v  # BIT
    if pa.types.is_string(arrow_type):
        return lambda v: v if v is None or isinstance(v, str) else (
            v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
        )
    return lambda v: v


@dataclass
class TableSnapshot:
    table: str  # logical (and Parquet file) name: the source table's name in snake_case
    path: Path
    num_rows: int
    renamed_columns: dict[str, str] = field(default_factory=dict)


class SnapshotTooLarge(UploadError):
    pass


def check_row_limit(cur: Any, quoted_tables: list[str], max_rows: int) -> None:
    """Exact row counts before copying anything: a 6,000,000-row database was refused only after
    5,000,000 rows had streamed (44 s, ops check 2026-10-11). COUNT(*) on a primary key takes about a
    second there; estimates (TABLE_ROWS, reltuples) can be off by tens of percent."""
    total = 0
    for table in quoted_tables:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        total += int(cur.fetchone()[0])
        if total > max_rows:
            raise SnapshotTooLarge(f"The database has more than {max_rows:,} rows, the snapshot limit")


def snapshot_tables(
    connect: Callable[[], Any],
    database: str,
    tables: list[str],
    workdir: Path,
    *,
    max_rows: int,
) -> list[TableSnapshot]:
    """Copy each source table into `<workdir>/<logical>.parquet`, where the logical name is the
    table's name in snake_case (`snake_identifier`: Jammi resolves only lowercase names unquoted).
    `connect` returns a PyMySQL connection. Stops with SnapshotTooLarge once the total passes
    max_rows, so a large database fails fast.
    """
    import pymysql.cursors

    snapshots: list[TableSnapshot] = []
    total = 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            check_row_limit(cur, ["`" + database.replace("`", "``") + "`.`" + t.replace("`", "``") + "`" for t in tables],
                            max_rows)
        columns = _columns(conn, database, tables)
        for table in tables:
            schema, renamed = _arrow_schema(columns.get(table, []))
            if not len(schema):
                continue
            converters = [_converter(f.type) for f in schema]
            logical = snapshot_table_name(table)
            path = workdir / f"{logical}.parquet"
            rows_written = 0
            # Not `with`: closing an unbuffered cursor reads every remaining row first. On an error the
            # connection is closed instead, which drops them.
            cur = conn.cursor(pymysql.cursors.SSCursor)
            with pq.ParquetWriter(path, schema, compression="zstd") as writer:
                select = ", ".join(f"`{c.name}`" for c in columns[table])
                cur.execute(f"SELECT {select} FROM `{table}`")
                while True:
                    rows = cur.fetchmany(_BATCH_ROWS)
                    if not rows:
                        break
                    total += len(rows)
                    if total > max_rows:
                        raise SnapshotTooLarge(
                            f"The database has more than {max_rows:,} rows, the snapshot limit"
                        )
                    arrays = [
                        pa.array([convert(row[i]) for row in rows], type=field_.type)
                        for i, (convert, field_) in enumerate(zip(converters, schema))
                    ]
                    writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
                    rows_written += len(rows)
            cur.close()
            snapshots.append(TableSnapshot(table=logical, path=path, num_rows=rows_written, renamed_columns=renamed))
    finally:
        conn.close()
    return snapshots


def snapshot_table_name(table: str) -> str:
    return snake_identifier(table, fallback="table", digit_prefix="t_")


def _columns(conn, database: str, tables: list[str]) -> dict[str, list[MySQLColumn]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, column_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns WHERE table_schema = %s ORDER BY table_name, ordinal_position",
            (database,),
        )
        wanted = set(tables)
        result: dict[str, list[MySQLColumn]] = {}
        for table, name, data_type, column_type, precision, scale in cur.fetchall():
            if table in wanted:
                result.setdefault(table, []).append(
                    MySQLColumn(name=name, data_type=str(data_type), column_type=str(column_type),
                                precision=precision, scale=scale)
                )
        return result


def _arrow_schema(columns: list[MySQLColumn]) -> tuple[pa.Schema, dict[str, str]]:
    names: list[str] = []
    renamed: dict[str, str] = {}
    fields = []
    for index, column in enumerate(columns):
        name = snake_identifier(column.name, fallback=f"column_{index + 1}", digit_prefix="c_")
        base, n = name, 2
        while name in names:
            name = f"{base}_{n}"
            n += 1
        names.append(name)
        if name != column.name:
            renamed[column.name] = name
        fields.append(pa.field(name, arrow_type_for(column)))
    return pa.schema(fields), renamed
