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


def test_postgres_connector_qualifies_schema_for_sampling_queries():
    connector = _build_connector("analytics")
    conn = RecordingConnection(
        [
            [("customer_id", "text", "YES")],
            [("sample",)],
        ]
    )

    connector._get_columns(conn, "orders")

    assert any('FROM "analytics"."orders"' in stmt for stmt in conn.cursor_instance.statements)


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


def test_postgres_connector_qualifies_schema_for_distinct_counts():
    connector = _build_connector("analytics")
    conn = RecordingConnection(
        [
            [],
            [(3,)],
        ]
    )

    counts = connector._get_distinct_counts(conn, "orders", [{"name": "customer_id"}])

    assert counts == {"customer_id": 3}
    assert any('FROM "analytics"."orders"' in stmt for stmt in conn.cursor_instance.statements)
