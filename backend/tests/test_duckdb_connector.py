from __future__ import annotations

import sys
from types import SimpleNamespace

from app.connectors.duckdb_connector import DuckDBConnector


class FakeDuckDBConnection:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, statement: str):
        self.statements.append(statement)
        return self

    def fetchall(self):
        return [("uploaded_table",)]


class FakeCredentials:
    access_key = "AKIA_TEST"
    secret_key = "SECRET'KEY"
    token = "SESSIONTOKEN"

    def get_frozen_credentials(self):
        return self


def test_s3_connector_configures_duckdb_with_aws_credentials(monkeypatch):
    fake_conn = FakeDuckDBConnection()

    monkeypatch.setattr("app.connectors.duckdb_connector.duckdb.connect", lambda _: fake_conn)
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(Session=lambda region_name=None: SimpleNamespace(get_credentials=lambda: FakeCredentials())),
    )

    connector = DuckDBConnector(
        db_path="s3://example-bucket/uploads/abc/test.csv",
        table_name="uploaded_table",
        aws_region="us-east-1",
    )

    connector._connect()

    assert any("SET s3_access_key_id='AKIA_TEST'" in stmt for stmt in fake_conn.statements)
    assert any("SET s3_secret_access_key='SECRET''KEY'" in stmt for stmt in fake_conn.statements)
    assert any("SET s3_session_token='SESSIONTOKEN'" in stmt for stmt in fake_conn.statements)
    assert any("read_csv_auto('s3://example-bucket/uploads/abc/test.csv')" in stmt for stmt in fake_conn.statements)
