from __future__ import annotations

from app.connectors.postgres_connector import PostgresConnector


class RecordingCursor:
    def __init__(self, result_sets):
        self._result_sets = [list(rows) for rows in result_sets]
        self._rows: list[tuple] = []
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        del params
        self.statements.append(statement)
        if self._result_sets:
            self._rows = self._result_sets.pop(0)
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class RecordingConnection:
    def __init__(self, result_sets):
        self.cursor_instance = RecordingCursor(result_sets)

    def cursor(self):
        return self.cursor_instance


def _build_connector(schema_name: str) -> PostgresConnector:
    connector = object.__new__(PostgresConnector)
    connector._schema_name = schema_name
    return connector


def test_postgres_connector_qualifies_schema_for_row_counts():
    connector = _build_connector("analytics")
    conn = RecordingConnection(
        [
            [(0,)],
            [(5,)],
        ]
    )

    row_count = connector._get_row_count(conn, "orders")

    assert row_count == 5
    assert any('SELECT COUNT(*) FROM "analytics"."orders"' in stmt for stmt in conn.cursor_instance.statements)


def test_the_preview_reads_columns_without_sampling_or_scanning_values():
    connector = _build_connector("analytics")
    conn = RecordingConnection([
        [("orders",)],                                                    # tables
        [("orders", "order_id", "text", "NO"), ("orders", "total", "numeric", "YES")],  # columns
        [(0,)], [(2,)],                                                   # row estimate, exact count
    ])
    connector._connect = lambda: type("C", (), {"cursor": conn.cursor, "close": lambda self: None})()

    schema = connector.get_schema()

    assert [c["name"] for c in schema.tables["orders"]] == ["order_id", "total"]
    assert schema.row_counts == {"orders": 2}
    statements = conn.cursor_instance.statements
    assert not any("DISTINCT" in s.upper() or "foreign key" in s.lower() for s in statements)


def test_sslmode_reaches_the_driver(monkeypatch):
    import sys
    import types

    import pytest

    seen = {}
    fake = types.SimpleNamespace(connect=lambda **kwargs: seen.update(kwargs))
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    params = {"host": "h", "dbname": "d", "user": "u", "password": "p"}
    PostgresConnector({**params, "sslmode": "verify-full"})._connect()
    assert seen["sslmode"] == "verify-full"
    with pytest.raises(ValueError, match="sslmode"):
        PostgresConnector({**params, "sslmode": "sometimes"})


def test_mysql_ssl_mode_reaches_the_driver(monkeypatch):
    import ssl
    import sys
    import types

    from app.connectors.mysql_connector import MySQLConnector

    seen = {}
    monkeypatch.setitem(sys.modules, "pymysql", types.SimpleNamespace(connect=lambda **kw: seen.update(kw)))
    params = {"host": "h", "dbname": "d", "user": "u", "password": "p"}
    MySQLConnector({**params, "ssl_mode": "verify_identity"})._connect()
    assert isinstance(seen["ssl"], ssl.SSLContext) and seen["ssl"].check_hostname
    seen.clear()
    MySQLConnector({**params, "ssl_mode": "required"})._connect()
    assert seen["ssl"].verify_mode == ssl.CERT_NONE
    seen.clear()
    MySQLConnector(params)._connect()
    assert "ssl" not in seen
