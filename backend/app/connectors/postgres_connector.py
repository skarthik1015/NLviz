from __future__ import annotations

import logging
import os
import re
from typing import Any

import pandas as pd

from .base import DataConnector, SchemaContext

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUERY_TIMEOUT_MS = int(os.environ.get("QUERY_TIMEOUT_MS", "30000"))


class PostgresConnector(DataConnector):
    """Full PostgreSQL connector using psycopg2."""

    def __init__(self, connection_params: dict[str, Any], denied_columns: list[str] | None = None):
        try:
            import psycopg2
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PostgreSQL connector requires psycopg2: pip install psycopg2-binary"
            ) from exc

        self._params = {
            "host": connection_params.get("host", "localhost"),
            "port": int(connection_params.get("port", 5432)),
            "dbname": connection_params["dbname"],
            "user": connection_params["user"],
            "password": connection_params["password"],
        }
        self._schema_name: str = connection_params.get("schema", "public")
        self._psycopg2 = psycopg2
        self._denied_columns: list[str] = denied_columns or []
        self._cached_allowed_tables: set[str] | None = None
        self._cached_table_columns: dict[str, set[str]] | None = None

    def get_connector_type(self) -> str:
        return "postgres"

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
            return True
        except Exception:
            return False

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        from app.security import validate_sql_safety

        normalized = sql.strip().rstrip(";")
        if not re.search(r"\blimit\s+\d+\b", normalized, flags=re.IGNORECASE):
            normalized = f"{normalized} LIMIT {limit}"

        validate_sql_safety(
            normalized,
            allowed_tables=self._get_allowed_tables(),
            table_columns=self._get_all_table_columns(),
            max_limit=limit,
            denied_columns=frozenset(self._denied_columns) if self._denied_columns else None,
            dialect="postgres",
        )

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = '{_QUERY_TIMEOUT_MS}'")
                cur.execute(normalized)
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
            return pd.DataFrame(rows, columns=columns)
        finally:
            conn.close()

    def get_schema(self) -> SchemaContext:
        conn = self._connect()
        try:
            tables = self._list_tables(conn)
            table_metadata: dict[str, list[dict]] = {}
            row_counts: dict[str, int] = {}
            distinct_counts: dict[str, dict[str, int]] = {}

            for table_name in tables:
                table_metadata[table_name] = self._get_columns(conn, table_name)
                row_counts[table_name] = self._get_row_count(conn, table_name)
                distinct_counts[table_name] = self._get_distinct_counts(
                    conn, table_name, table_metadata[table_name]
                )

            # Priority 1: declared FK metadata
            fk_joins, fk_provenance = self._get_fk_joins(conn)
            # Priority 2: heuristic inference for tables without FK coverage
            heuristic_joins, heuristic_provenance = self._infer_heuristic_joins(
                table_metadata, set(tables), fk_joins
            )

            join_provenance = {**heuristic_provenance, **fk_provenance}

            return SchemaContext(
                tables=table_metadata,
                row_counts=row_counts,
                join_paths=fk_joins,
                distinct_counts=distinct_counts,
                inferred_joins=heuristic_joins,
                join_provenance=join_provenance,
            )
        finally:
            conn.close()

    def close(self) -> None:
        return None

    # ── Private helpers ──────────────────────────────────────────────

    def _connect(self):
        return self._psycopg2.connect(**self._params, connect_timeout=5)

    def _quote(self, identifier: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError(f"Unsafe identifier: {identifier}")
        return f'"{identifier}"'

    def _get_allowed_tables(self) -> set[str]:
        if self._cached_allowed_tables is None:
            conn = self._connect()
            try:
                self._cached_allowed_tables = set(self._list_tables(conn))
            finally:
                conn.close()
        return self._cached_allowed_tables

    def _get_all_table_columns(self) -> dict[str, set[str]]:
        if self._cached_table_columns is None:
            conn = self._connect()
            try:
                result: dict[str, set[str]] = {}
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = %s",
                        (self._schema_name,),
                    )
                    for table_name, column_name in cur.fetchall():
                        result.setdefault(table_name, set()).add(column_name)
                self._cached_table_columns = result
            finally:
                conn.close()
        return self._cached_table_columns

    def _list_tables(self, conn) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                (self._schema_name,),
            )
            return [row[0] for row in cur.fetchall()]

    def _get_columns(self, conn, table_name: str) -> list[dict]:
        safe_table = self._quote(table_name)
        columns: list[dict] = []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (self._schema_name, table_name),
            )
            for col_name, data_type, is_nullable in cur.fetchall():
                safe_col = self._quote(col_name)
                # Sample values
                try:
                    cur.execute(
                        f"SELECT DISTINCT {safe_col} FROM {safe_table} "
                        f"WHERE {safe_col} IS NOT NULL LIMIT 5"
                    )
                    samples = [row[0] for row in cur.fetchall()]
                except Exception:
                    samples = []
                columns.append({
                    "name": col_name,
                    "type": data_type,
                    "nullable": is_nullable == "YES",
                    "sample_values": samples,
                })
        return columns

    def _get_row_count(self, conn, table_name: str) -> int:
        """Use pg_stat estimate for large tables, exact for small."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reltuples::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = %s AND n.nspname = %s",
                (table_name, self._schema_name),
            )
            row = cur.fetchone()
            estimate = row[0] if row else 0
            if estimate < 100_000:
                safe_table = self._quote(table_name)
                cur.execute(f"SELECT COUNT(*) FROM {safe_table}")
                exact = cur.fetchone()
                return exact[0] if exact else 0
            return max(estimate, 0)

    def _get_distinct_counts(
        self, conn, table_name: str, columns: list[dict]
    ) -> dict[str, int]:
        ndv: dict[str, int] = {}
        safe_table = self._quote(table_name)
        for col in columns:
            col_name = col["name"]
            safe_col = self._quote(col_name)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = '10s'")
                    cur.execute(
                        f"SELECT COUNT(DISTINCT {safe_col}) FROM {safe_table}"
                    )
                    result = cur.fetchone()
                    ndv[col_name] = result[0] if result else 0
            except Exception:
                # Fallback to pg_stats n_distinct
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT n_distinct FROM pg_stats "
                            "WHERE schemaname = %s AND tablename = %s AND attname = %s",
                            (self._schema_name, table_name, col_name),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            val = row[0]
                            # Negative means fraction of rows
                            ndv[col_name] = int(abs(val)) if val > 0 else -1
                        else:
                            ndv[col_name] = -1
                except Exception:
                    ndv[col_name] = -1
        return ndv

    def _get_fk_joins(self, conn) -> tuple[list[dict], dict[str, str]]:
        """Extract declared foreign key relationships."""
        joins: list[dict] = []
        provenance: dict[str, str] = {}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    kcu.table_name AS from_table,
                    kcu.column_name AS from_column,
                    ccu.table_name AS to_table,
                    ccu.column_name AS to_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                    AND tc.table_schema = ccu.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = %s
                """,
                (self._schema_name,),
            )
            for from_table, from_col, to_table, to_col in cur.fetchall():
                join = {
                    "from": from_table,
                    "to": to_table,
                    "on": f"{from_table}.{from_col} = {to_table}.{to_col}",
                    "type": "LEFT",
                }
                joins.append(join)
                key = f"{from_table}.{from_col}={to_table}.{to_col}"
                provenance[key] = "fk"
        return joins, provenance

    @staticmethod
    def _infer_heuristic_joins(
        table_metadata: dict[str, list[dict]],
        table_set: set[str],
        fk_joins: list[dict],
    ) -> tuple[list[dict], dict[str, str]]:
        """Heuristic FK inference for tables not covered by declared FKs."""
        fk_pairs: set[tuple[str, str]] = set()
        for j in fk_joins:
            pair = (min(j["from"], j["to"]), max(j["from"], j["to"]))
            fk_pairs.add(pair)

        inferred: list[dict] = []
        provenance: dict[str, str] = {}

        for table_name, columns in table_metadata.items():
            for col in columns:
                col_name: str = col["name"]
                if not col_name.endswith("_id"):
                    continue
                stem = col_name[: -len("_id")]
                for candidate in [stem, stem + "s", stem + "es"]:
                    if candidate in table_set and candidate != table_name:
                        pair = (min(table_name, candidate), max(table_name, candidate))
                        if pair in fk_pairs:
                            break
                        fk_pairs.add(pair)
                        target_cols = {c["name"] for c in table_metadata.get(candidate, [])}
                        if col_name in target_cols:
                            inferred.append({
                                "from": table_name,
                                "to": candidate,
                                "on": f"{table_name}.{col_name} = {candidate}.{col_name}",
                                "type": "LEFT",
                            })
                            key = f"{table_name}.{col_name}={candidate}.{col_name}"
                            provenance[key] = "heuristic"
                        break

        return inferred, provenance
