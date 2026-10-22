"""DataConnector backed by jammi-server (one NLviz connection = a set of Jammi sources).

The connector is constructed with the connection's table mapping, logical name -> physical
reference ("orders" -> "w1a2_c3b4_orders.public.orders"). That mapping is the single source of
truth for three things that must agree:
  * table_ref()      - what the SQL compiler emits for each logical table;
  * allowed_tables   - the SQL safety allowlist (workspace isolation, decision AD-5);
  * get_schema()     - which tables are introspected (Jammi cannot list them remotely, J22).
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pandas as pd
import pyarrow as pa

from app.jammi.client import JammiClient
from app.security import validate_sql_safety

from .base import DataConnector, SchemaContext
from .join_inference import infer_joins

_LOGICAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Lowercase only: the safety gate folds unquoted identifiers to lowercase, so the stored
# physical name must already be lowercase to match what the compiler emits.
_PHYSICAL_NAME = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*")
_LIMIT_CLAUSE = re.compile(r"\blimit\s+\d+\b", flags=re.IGNORECASE)

_ARROW_TO_SQL = {
    "Utf8": "VARCHAR", "Utf8View": "VARCHAR", "LargeUtf8": "VARCHAR",
    "Int8": "TINYINT", "Int16": "SMALLINT", "Int32": "INTEGER", "Int64": "BIGINT",
    "UInt8": "UTINYINT", "UInt16": "USMALLINT", "UInt32": "UINTEGER", "UInt64": "UBIGINT",
    "Float16": "REAL", "Float32": "REAL", "Float64": "DOUBLE", "Boolean": "BOOLEAN",
    "Date32": "DATE", "Date64": "DATE", "Binary": "BLOB", "BinaryView": "BLOB",
    "LargeBinary": "BLOB", "Null": "NULL",
}


def arrow_type_to_sql(data_type: str) -> str:
    """Map a DataFusion DESCRIBE data_type to the SQL type names the schema generator expects."""
    if data_type in _ARROW_TO_SQL:
        return _ARROW_TO_SQL[data_type]
    if data_type.startswith("Timestamp("):
        return "TIMESTAMP WITH TIME ZONE" if "," in data_type else "TIMESTAMP"
    if data_type.startswith(("Time32", "Time64")):
        return "TIME"
    if data_type.startswith("Decimal"):
        match = re.search(r"\((\d+),\s*(-?\d+)\)", data_type)
        return f"DECIMAL({match.group(1)},{match.group(2)})" if match else "DECIMAL"
    if data_type.startswith("Interval"):
        return "INTERVAL"
    if data_type.startswith(("List", "LargeList", "FixedSizeList")):
        return "LIST"
    if data_type.startswith("Struct"):
        return "STRUCT"
    if data_type.startswith("Dictionary") and "Utf8" in data_type:
        return "VARCHAR"
    return data_type


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ndv_expression(column: str, data_type: str, *, exact: bool = False) -> str:
    # DataFusion's approx_distinct is not implemented for Float64/Decimal (issue J19). On a live
    # Postgres source Jammi pushes approx_distinct down verbatim and Postgres has no such function
    # (issue J31): `exact` counts with COUNT(DISTINCT), which every source supports.
    quoted = _quote_identifier(column)
    if exact:
        return f"COUNT(DISTINCT {quoted})"
    if data_type.startswith(("Float", "Decimal")):
        return f"approx_distinct(CAST({quoted} AS VARCHAR))"
    return f"approx_distinct({quoted})"


def _sample_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, (datetime, date, time, Decimal)):
        return str(value)
    return str(value)


class CrossSourceQueryError(ValueError):
    """A query mixes a live database source with a file source (unsafe on Jammi 0.54, issue J28)."""


class JammiConnector(DataConnector):
    def __init__(
        self,
        client: JammiClient,
        tables: Mapping[str, str],
        *,
        denied_columns: list[str] | None = None,
        sample_size: int = 5,
        max_workers: int = 4,
        database_sources: set[str] | frozenset[str] = frozenset(),
        declared_joins: list[dict] | None = None,
        hidden_columns: list[str] | None = None,
    ):
        if not tables:
            raise ValueError("JammiConnector needs at least one table")
        self._database_sources = frozenset(database_sources)
        for logical, physical in tables.items():
            if not _LOGICAL_NAME.fullmatch(logical):
                raise ValueError(f"Invalid logical table name: {logical!r}")
            if not _PHYSICAL_NAME.fullmatch(physical):
                raise ValueError(f"Invalid physical table reference for {logical!r}: {physical!r}")
        self._client = client
        self._tables = dict(tables)
        # Foreign keys the source database declares (read at connection time), between known tables.
        self._declared_joins = [j for j in (declared_joins or [])
                                if j.get("from") in self._tables and j.get("to") in self._tables]
        self._denied_columns = list(denied_columns or [])
        # "table.column" the source cannot serve (J36: pre-1970 timestamps on live Postgres): never
        # described, sampled or counted, so schema generation cannot propose them.
        self._hidden = {tuple(c.split(".", 1)) for c in (hidden_columns or []) if "." in c}
        self._sample_size = sample_size
        self._max_workers = max_workers
        self._columns_lock = threading.Lock()
        self._cached_columns: dict[str, list[dict[str, str]]] | None = None

    # --- mapping -------------------------------------------------------------------------

    @property
    def tables(self) -> dict[str, str]:
        """Logical name -> physical "<source>.public.<table>" (a copy)."""
        return dict(self._tables)

    def table_ref(self, logical_table: str) -> str:
        """Compiler resolver (app.semantic.sql_builder.TableRefResolver)."""
        try:
            return self._tables[logical_table]
        except KeyError:
            raise ValueError(f"Table {logical_table!r} is not part of this connection") from None

    def table_ref_resolver(self):
        return self.table_ref

    @property
    def allowed_tables(self) -> set[str]:
        return set(self._tables.values())

    # --- DataConnector -------------------------------------------------------------------

    def get_connector_type(self) -> str:
        return "jammi"

    def test_connection(self) -> bool:
        return self._client.ping()

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        normalized = sql.strip().rstrip(";")
        if not _LIMIT_CLAUSE.search(normalized):
            normalized = f"{normalized} LIMIT {limit}"
        checked = validate_sql_safety(
            normalized,
            allowed_tables=self.allowed_tables,
            table_columns={physical: {c["column_name"] for c in cols} for physical, cols in self._columns().items()},
            max_limit=limit,
            denied_columns=frozenset(self._denied_columns) if self._denied_columns else None,
        )
        sources = {table.split(".", 1)[0] for table in checked.tables}
        if sources & self._database_sources and sources - self._database_sources:
            raise CrossSourceQueryError(
                "CROSS_SOURCE_UNSAFE: this question combines the live database with an attached file. "
                "Jammi 0.54 can drop filters on such queries and return wrong numbers (Jammi issue J28), "
                "so they are not run. Ask about the database or the file separately."
            )
        table = self._client.sql(normalized)
        if table.num_columns == 0:
            # The embedded engine drops the schema of empty results (issue J5); the remote client
            # keeps it. Recover the column names so empty charts/tables still have headers.
            names = [row["column_name"] for row in self._client.describe(normalized)]
            return pd.DataFrame(columns=names)
        return table.to_pandas()

    def get_schema(self) -> SchemaContext:
        columns_by_physical = self._columns()
        table_metadata: dict[str, list[dict]] = {}
        row_counts: dict[str, int] = {}
        distinct_counts: dict[str, dict[str, int]] = {}

        def introspect(logical: str) -> tuple[str, list[dict], int, dict[str, int]]:
            physical = self._tables[logical]
            described = [c for c in columns_by_physical[physical] if (logical, c["column_name"]) not in self._hidden]
            rows, ndv = self._row_and_distinct_counts(physical, described)
            columns = [
                {
                    "name": column["column_name"],
                    "type": arrow_type_to_sql(column["data_type"]),
                    "nullable": column["is_nullable"] == "YES",
                    "sample_values": self._sample_values(physical, column["column_name"]),
                }
                for column in described
            ]
            return logical, columns, rows, ndv

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            for logical, columns, rows, ndv in pool.map(introspect, sorted(self._tables)):
                table_metadata[logical] = columns
                row_counts[logical] = rows
                distinct_counts[logical] = ndv

        inferred_joins, join_provenance = infer_joins(table_metadata, set(table_metadata), distinct_counts, row_counts)
        # Declared foreign keys win; the name heuristic only adds pairs they leave unjoined
        # (customer.support_rep_id -> employee has no "_id" match; Chinook declares it).
        joinable = [j for j in self._declared_joins if j["from"] != j["to"]]
        declared_pairs = {frozenset((j["from"], j["to"])) for j in joinable}
        inferred_joins = [j for j in inferred_joins if frozenset((j["from"], j["to"])) not in declared_pairs]
        join_provenance.update({j["on"]: "fk" for j in joinable})
        key_columns = {(m.group(1), m.group(2)) for j in self._declared_joins + inferred_joins
                       for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", j["on"])}
        return SchemaContext(
            tables=table_metadata,
            row_counts=row_counts,
            join_paths=joinable,
            distinct_counts=distinct_counts,
            inferred_joins=inferred_joins,
            join_provenance=join_provenance,
            key_columns=key_columns,
        )

    def close(self) -> None:
        # The JammiClient is process-wide and shared; nothing per-connector to release.
        return None

    # --- helpers -------------------------------------------------------------------------

    def _columns(self) -> dict[str, list[dict[str, str]]]:
        with self._columns_lock:
            if self._cached_columns is None:
                self._cached_columns = {
                    physical: self._client.describe(physical) for physical in sorted(set(self._tables.values()))
                }
            return self._cached_columns

    def _row_and_distinct_counts(self, physical: str, columns: list[dict[str, str]]) -> tuple[int, dict[str, int]]:
        names = [column["column_name"] for column in columns]
        for exact in (False, True):  # approximate first (cheap on files); exact where that fails (J31)
            projections = ", ".join(
                f"{_ndv_expression(column['column_name'], column['data_type'], exact=exact)} AS ndv_{index}"
                for index, column in enumerate(columns)
            )
            select = f"SELECT COUNT(*) AS row_count{', ' + projections if projections else ''} FROM {physical}"
            try:
                row = self._client.sql(select).to_pylist()[0]
                return int(row["row_count"]), {name: int(row[f"ndv_{i}"]) for i, name in enumerate(names)}
            except Exception:
                continue
        # One unsupported column type must not hide the others: per column, recording -1 like
        # DuckDBConnector does.
        rows = int(self._client.sql(f"SELECT COUNT(*) AS row_count FROM {physical}").to_pylist()[0]["row_count"])
        ndv: dict[str, int] = {}
        for column in columns:
            ndv[column["column_name"]] = -1
            for exact in (False, True):
                expression = _ndv_expression(column["column_name"], column["data_type"], exact=exact)
                try:
                    ndv[column["column_name"]] = int(
                        self._client.sql(f"SELECT {expression} AS ndv FROM {physical}").to_pylist()[0]["ndv"])
                    break
                except Exception:
                    continue
        return rows, ndv

    def _sample_values(self, physical: str, column: str) -> list[Any]:
        quoted = _quote_identifier(column)
        try:
            table: pa.Table = self._client.sql(
                f"SELECT DISTINCT {quoted} AS v FROM {physical} WHERE {quoted} IS NOT NULL LIMIT {self._sample_size}"
            )
        except Exception:
            return []
        return [_sample_value(value) for value in table.column("v").to_pylist()] if table.num_columns else []
