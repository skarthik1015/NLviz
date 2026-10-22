"""MySQL discovery for connections that Jammi federates.

NLviz never queries MySQL directly: Jammi does, through a `mysql://` source. This connector only
tests the credentials and lists the database's tables and columns (Jammi cannot list them for a
remote client, issue J22), for the connection test preview and for registration.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .base import DataConnector, SchemaContext

_CONNECT_TIMEOUT_S = 10
# MySQL's own names for the client's TLS choice (--ssl-mode). "preferred" is MySQL's default, but
# PyMySQL cannot fall back from TLS to plain text, so it is treated as "disabled"; ask for
# "required" to encrypt.
_SSL_MODES = {"disabled", "preferred", "required", "verify_ca", "verify_identity"}


def _ssl_context(mode: str | None):
    """The SSLContext for an ssl mode, or None for a plain connection."""
    if mode in (None, "", "disabled", "preferred"):
        return None
    import ssl

    context = ssl.create_default_context()
    if mode == "required":  # encrypted, server not verified (MySQL's REQUIRED)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify_ca":
        context.check_hostname = False
    return context


class MySQLConnector(DataConnector):
    def __init__(self, connection_params: dict[str, Any], denied_columns: list[str] | None = None):
        try:
            import pymysql
        except ModuleNotFoundError as exc:
            raise RuntimeError("MySQL discovery requires PyMySQL: pip install PyMySQL") from exc
        self._pymysql = pymysql
        self._params = {
            "host": connection_params.get("host", "localhost"),
            "port": int(connection_params.get("port", 3306)),
            "database": connection_params["dbname"],
            "user": connection_params["user"],
            "password": connection_params.get("password", ""),
        }
        # The snapshot is read with this driver (MySQL is never federated by default, J27), so the
        # user's TLS choice applies here; it was ignored before (review).
        ssl_mode = connection_params.get("ssl_mode") or connection_params.get("sslmode")
        if ssl_mode and ssl_mode not in _SSL_MODES:
            raise ValueError(f"ssl_mode must be one of {', '.join(sorted(_SSL_MODES))}")
        self._ssl = _ssl_context(ssl_mode)

    def get_connector_type(self) -> str:
        return "mysql"

    def _connect(self):
        return self._pymysql.connect(
            **self._params,
            connect_timeout=_CONNECT_TIMEOUT_S,
            read_timeout=30,
            charset="utf8mb4",
            **({"ssl": self._ssl} if self._ssl is not None else {}),
        )

    def test_connection(self) -> bool:
        conn = self._connect()  # let exceptions propagate with their message
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
        finally:
            conn.close()

    def list_tables(self) -> list[str]:
        """Tables and views of the connection's database: Jammi's MySQL source exposes both
        (it discovers with SHOW TABLES)."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
                    (self._params["database"],),
                )
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def get_schema(self) -> SchemaContext:
        """Cheap preview for the connection test: columns, and MySQL's estimated row counts."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema = %s",
                    (self._params["database"],),
                )
                row_counts = {name: int(rows or 0) for name, rows in cur.fetchall()}
                cur.execute(
                    "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
                    (self._params["database"],),
                )
                tables: dict[str, list[dict]] = {name: [] for name in row_counts}
                for table, column, data_type, nullable in cur.fetchall():
                    tables.setdefault(table, []).append(
                        {"name": column, "type": data_type, "nullable": nullable == "YES"}
                    )
        finally:
            conn.close()
        return SchemaContext(tables=tables, row_counts=row_counts, join_paths=[])

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        raise NotImplementedError("MySQL connections are queried through Jammi")
