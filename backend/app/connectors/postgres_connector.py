"""PostgreSQL discovery for connections that Jammi federates.

NLviz never runs a question's SQL on Postgres itself: Jammi does, through a `postgresql://` source.
This connector tests the credentials, lists the tables (Jammi cannot list them for a remote client,
issue J22) and gives the connection test its preview. Foreign keys and the snapshot copy are read
by the connection routes and app/services/postgres_snapshot.py with this connector's connection.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from .base import DataConnector, SchemaContext

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


class PostgresConnector(DataConnector):
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
        # The same TLS choice Jammi's source URL carries (jammi_sources.postgres_url): discovery and
        # snapshots must not connect less securely than the queries do.
        sslmode = connection_params.get("sslmode")
        if sslmode:
            if sslmode not in _SSL_MODES:
                raise ValueError(f"sslmode must be one of {', '.join(sorted(_SSL_MODES))}")
            self._params["sslmode"] = sslmode
        self._schema_name: str = connection_params.get("schema", "public")
        self._psycopg2 = psycopg2

    def get_connector_type(self) -> str:
        return "postgres"

    def test_connection(self) -> bool:
        conn = self._connect()  # let exceptions propagate with their message
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        raise NotImplementedError("Postgres connections are queried through Jammi")

    def get_schema(self) -> SchemaContext:
        """Cheap preview for the connection test: columns, and the planner's row estimates (exact
        counts for small tables)."""
        conn = self._connect()
        try:
            tables = self._list_tables(conn)
            columns: dict[str, list[dict]] = {name: [] for name in tables}
            row_counts: dict[str, int] = {}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
                    (self._schema_name,),
                )
                for table, column, data_type, nullable in cur.fetchall():
                    if table in columns:
                        columns[table].append({"name": column, "type": data_type, "nullable": nullable == "YES"})
            for table in tables:
                row_counts[table] = self._get_row_count(conn, table)
            return SchemaContext(tables=columns, row_counts=row_counts, join_paths=[])
        finally:
            conn.close()

    def close(self) -> None:
        return None

    def list_tables(self) -> list[str]:
        """Base tables of the configured schema (used to register the database with Jammi)."""
        conn = self._connect()
        try:
            return self._list_tables(conn)
        finally:
            conn.close()

    # ── Private helpers ──────────────────────────────────────────────

    def _connect(self):
        return self._psycopg2.connect(**self._params, connect_timeout=5)

    def _quote(self, identifier: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError(f"Unsafe identifier: {identifier}")
        return f'"{identifier}"'

    def _qualified_table(self, table_name: str) -> str:
        return f'{self._quote(self._schema_name)}.{self._quote(table_name)}'

    def _list_tables(self, conn) -> list[str]:
        """Tables and partitioned tables the user can read, without the partitions themselves:
        Pagila's payment has 7 monthly partitions, which information_schema lists as 7 more tables
        (every payment would be counted twice through them)."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
                "AND has_table_privilege(c.oid, 'SELECT') ORDER BY c.relname",
                (self._schema_name,),
            )
            return [row[0] for row in cur.fetchall()]

    def _get_row_count(self, conn, table_name: str) -> int:
        """The planner's estimate for large tables, an exact count for small ones."""
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
                safe_table = self._qualified_table(table_name)
                cur.execute(f"SELECT COUNT(*) FROM {safe_table}")
                exact = cur.fetchone()
                return exact[0] if exact else 0
            return max(estimate, 0)
