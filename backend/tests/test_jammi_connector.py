"""Unit tests for JammiConnector / JammiClient with a fake client (no server needed).

The live-server tests are in test_jammi_integration.py.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from app.connectors.jammi_connector import JammiConnector, arrow_type_to_sql
from app.jammi.client import JammiClient, JammiConfigError, JammiSettings
from app.models import SemanticIntent
from app.security import SQLSafetyError
from app.semantic import build_sql_from_intent, load_semantic_registry

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"

TABLES = {
    "orders": "wa_c1_orders.public.orders",
    "customers": "wa_c1_customers.public.customers",
}
DESCRIBED = {
    "wa_c1_orders.public.orders": [
        {"column_name": "order_id", "data_type": "Utf8View", "is_nullable": "YES"},
        {"column_name": "customer_id", "data_type": "Utf8View", "is_nullable": "YES"},
        {"column_name": "order_purchase_timestamp", "data_type": "Timestamp(µs)", "is_nullable": "YES"},
        {"column_name": "freight", "data_type": "Float64", "is_nullable": "NO"},
    ],
    "wa_c1_customers.public.customers": [
        {"column_name": "customer_id", "data_type": "Utf8View", "is_nullable": "YES"},
        {"column_name": "customer_state", "data_type": "Utf8View", "is_nullable": "YES"},
        {"column_name": "customer_unique_id", "data_type": "Utf8View", "is_nullable": "YES"},
    ],
}


class FakeClient:
    """Answers the SQL shapes JammiConnector issues; records every statement."""

    def __init__(self, *, fail_one_pass_ndv: bool = False, empty_result: bool = False):
        self.statements: list[str] = []
        self.fail_one_pass_ndv = fail_one_pass_ndv
        self.empty_result = empty_result

    def describe(self, relation_or_query: str):
        self.statements.append(f"DESCRIBE {relation_or_query}")
        if relation_or_query in DESCRIBED:
            return DESCRIBED[relation_or_query]
        return [{"column_name": "customer_state", "data_type": "Utf8View", "is_nullable": "YES"},
                {"column_name": "metric_value", "data_type": "Int64", "is_nullable": "NO"}]

    def ping(self) -> bool:
        return True

    def sql(self, query: str) -> pa.Table:
        self.statements.append(query)
        if query.startswith("SELECT COUNT(*) AS row_count,"):
            if self.fail_one_pass_ndv:
                raise RuntimeError("approx_distinct not implemented for some type")
            n = len(re.findall(r" AS ndv_\d+", query))
            return pa.table({"row_count": [10], **{f"ndv_{i}": [i + 1] for i in range(n)}})
        if query.startswith("SELECT COUNT(*) AS row_count FROM"):
            return pa.table({"row_count": [10]})
        if query.startswith("SELECT approx_distinct("):
            if "freight" in query:
                raise RuntimeError("unsupported")
            return pa.table({"ndv": [7]})
        if query.startswith("SELECT DISTINCT"):
            return pa.table({"v": ["a", "b"]})
        if self.empty_result:
            return pa.table({})
        return pa.table({"customer_state": ["SP", "RJ"], "metric_value": [2, 1]})


def _connector(**kwargs) -> tuple[JammiConnector, FakeClient]:
    client = FakeClient(**kwargs)
    return JammiConnector(client, TABLES, denied_columns=["customer_unique_id"]), client  # type: ignore[arg-type]


# --- construction and mapping ---


@pytest.mark.parametrize(
    "tables",
    [
        {},
        {"orders": "orders"},  # not fully qualified
        {"orders": "WA_C1.public.orders"},  # uppercase would not match the gate's case folding
        {"orders": "a.b.c.d"},
        {"orders": "x.public.orders; DROP TABLE y"},
        {"bad-name": "a.public.b"},
    ],
)
def test_rejects_invalid_table_mapping(tables):
    with pytest.raises(ValueError):
        JammiConnector(FakeClient(), tables)  # type: ignore[arg-type]


def test_table_ref_and_allowlist_come_from_one_mapping():
    connector, _ = _connector()
    assert connector.table_ref("orders") == "wa_c1_orders.public.orders"
    assert connector.allowed_tables == set(TABLES.values())
    with pytest.raises(ValueError, match="not part of this connection"):
        connector.table_ref("sellers")
    assert connector.get_connector_type() == "jammi"
    assert connector.test_connection() is True


# --- execute_query ---


def test_execute_query_runs_allowed_sql_and_returns_dataframe():
    connector, client = _connector()
    df = connector.execute_query(
        "SELECT c.customer_state, COUNT(*) AS metric_value FROM wa_c1_orders.public.orders AS orders "
        "JOIN wa_c1_customers.public.customers AS c ON orders.customer_id = c.customer_id GROUP BY c.customer_state",
        limit=50,
    )
    assert list(df.columns) == ["customer_state", "metric_value"]
    assert client.statements[-1].endswith("LIMIT 50")


def test_other_workspace_query_is_refused_before_reaching_jammi():
    connector, client = _connector()
    with pytest.raises(SQLSafetyError, match="outside allowlist"):
        connector.execute_query("SELECT COUNT(*) FROM wb_c9_orders.public.orders LIMIT 5")
    assert not any("wb_c9" in s for s in client.statements)


def test_denied_column_through_wildcard_uses_described_columns():
    connector, client = _connector()
    with pytest.raises(SQLSafetyError, match="wildcard"):
        connector.execute_query("SELECT c.* FROM wa_c1_customers.public.customers AS c LIMIT 5")
    assert not any(s.startswith("SELECT c.*") for s in client.statements)


def test_empty_result_without_schema_recovers_column_names():
    connector, _ = _connector(empty_result=True)
    df = connector.execute_query(
        "SELECT c.customer_state, COUNT(*) AS metric_value FROM wa_c1_customers.public.customers AS c "
        "GROUP BY c.customer_state LIMIT 5"
    )
    assert df.empty
    assert list(df.columns) == ["customer_state", "metric_value"]


def test_compiled_sql_with_connector_resolver_passes_the_gate():
    connector, client = _connector()
    registry = load_semantic_registry(SCHEMA_PATH)
    sql = build_sql_from_intent(
        SemanticIntent(metric="order_count", limit=10), registry, table_ref=connector.table_ref
    )
    connector.execute_query(sql, limit=10)
    assert "FROM wa_c1_orders.public.orders AS orders" in client.statements[-1]


# --- get_schema ---


def test_get_schema_shape_matches_duckdb_connector_contract():
    connector, _ = _connector()
    schema = connector.get_schema()

    assert set(schema.tables) == {"orders", "customers"}
    orders_cols = {c["name"]: c for c in schema.tables["orders"]}
    assert orders_cols["order_purchase_timestamp"]["type"] == "TIMESTAMP"
    assert orders_cols["freight"] == {"name": "freight", "type": "DOUBLE", "nullable": False, "sample_values": ["a", "b"]}
    assert schema.row_counts == {"orders": 10, "customers": 10}
    assert schema.distinct_counts["orders"] == {
        "order_id": 1, "customer_id": 2, "order_purchase_timestamp": 3, "freight": 4,
    }
    assert {"from": "orders", "to": "customers", "on": "orders.customer_id = customers.customer_id",
            "type": "LEFT", "relationship": "many_to_one"} in schema.inferred_joins


def test_get_schema_casts_float_columns_for_approx_distinct():
    connector, client = _connector()
    connector.get_schema()
    one_pass = next(s for s in client.statements if s.startswith("SELECT COUNT(*) AS row_count,") and "orders" in s)
    assert 'approx_distinct(CAST("freight" AS VARCHAR))' in one_pass
    assert 'approx_distinct("order_id")' in one_pass


def test_get_schema_falls_back_per_column_and_records_minus_one():
    connector, _ = _connector(fail_one_pass_ndv=True)
    schema = connector.get_schema()
    assert schema.distinct_counts["orders"]["order_id"] == 7
    assert schema.distinct_counts["orders"]["freight"] == -1
    assert schema.row_counts["orders"] == 10


def test_quoted_identifiers_escape_embedded_quotes():
    client = FakeClient()
    DESCRIBED["wa_c1_odd.public.odd"] = [{"column_name": 'we"ird col', "data_type": "Utf8", "is_nullable": "YES"}]
    try:
        JammiConnector(client, {"odd": "wa_c1_odd.public.odd"}).get_schema()  # type: ignore[arg-type]
    finally:
        del DESCRIBED["wa_c1_odd.public.odd"]
    assert any('"we""ird col"' in s for s in client.statements)


@pytest.mark.parametrize(
    ("arrow", "sql"),
    [("Utf8View", "VARCHAR"), ("Int64", "BIGINT"), ("Float64", "DOUBLE"), ("Timestamp(µs)", "TIMESTAMP"),
     ('Timestamp(µs, "+00:00")', "TIMESTAMP WITH TIME ZONE"), ("Decimal128(10, 2)", "DECIMAL(10,2)"),
     ("Date32", "DATE"), ("Boolean", "BOOLEAN")],
)
def test_arrow_type_mapping(arrow, sql):
    assert arrow_type_to_sql(arrow) == sql


# --- settings and client ---


def _config(**overrides):
    from app.config import AppConfig

    base = dict(
        api_prefix="", database_url=None, auto_migrate=False, db_pool_min=1, db_pool_max=1,
        secret_backend="local", secret_prefix="x", upload_bucket=None, schema_bucket=None,
        aws_region="us-east-1", cors_origins=(), rate_limit_rpm=30, environment="test",
        log_level="INFO", dev_user_id=None,
    )
    base.update(overrides)
    return AppConfig(**base)


def test_settings_from_config_validate_url_and_v4_or_v7_tenant():
    assert JammiSettings.from_config(_config()) is None  # Jammi not configured: legacy engine
    with pytest.raises(JammiConfigError, match="JAMMI_SERVICE_TENANT"):
        JammiSettings.from_config(_config(jammi_url="grpc://jammi:8081"))
    with pytest.raises(JammiConfigError, match="not a UUID"):
        JammiSettings.from_config(_config(jammi_url="grpc://jammi:8081", jammi_service_tenant="nope"))
    with pytest.raises(JammiConfigError, match="v4 or v7"):
        JammiSettings.from_config(_config(jammi_url="grpc://jammi:8081", jammi_service_tenant=str(uuid.uuid1())))
    tenant = str(uuid.uuid4())
    assert JammiSettings.from_config(_config(jammi_url="grpc://jammi:8081", jammi_service_tenant=tenant)) == (
        JammiSettings(url="grpc://jammi:8081", service_tenant=tenant)
    )


def test_app_config_reads_jammi_env(monkeypatch):
    from app.config import AppConfig

    monkeypatch.setenv("JAMMI_URL", "grpc://jammi:8081")
    monkeypatch.setenv("JAMMI_SERVICE_TENANT", "t")
    monkeypatch.setenv("JAMMI_DEMO_DATA_URL", "/data/olist")
    config = AppConfig.from_env()
    assert (config.jammi_url, config.jammi_service_tenant, config.jammi_demo_data_url) == (
        "grpc://jammi:8081", "t", "/data/olist",
    )


def test_client_binds_service_tenant_and_warms_flight_before_use():
    calls: list[tuple[str, object]] = []

    class FakeRemote:
        def set_tenant(self, tenant):
            calls.append(("set_tenant", tenant))

        def sql(self, query):
            calls.append(("sql", query))
            return pa.table({"x": [1]})

    tenant = str(uuid.uuid4())
    client = JammiClient(JammiSettings("grpc://jammi:8081", tenant), connect=lambda url: FakeRemote())
    assert calls == [("set_tenant", tenant), ("sql", "SELECT 1")]
    assert client.service_tenant == tenant
    assert isinstance(client.sql("SELECT 2"), pa.Table)
    assert pd.api.types.is_integer_dtype(client.sql("SELECT 3").to_pandas()["x"])


def test_connect_jammi_retries_until_the_server_is_up(monkeypatch):
    import app.dependencies as deps

    monkeypatch.setattr(deps.time, "sleep", lambda s: None)
    attempts = []

    def flaky(settings):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("connection refused")
        return "client"

    assert deps.connect_jammi("settings", connect=flaky) == "client" and len(attempts) == 3


def test_connect_jammi_gives_up_after_the_deadline(monkeypatch):
    import app.dependencies as deps

    monkeypatch.setattr(deps.time, "sleep", lambda s: None)

    def down(settings):
        raise ConnectionError("connection refused")

    with pytest.raises(ConnectionError):
        deps.connect_jammi("settings", wait_s=0, connect=down)


# --- J28: no query mixes a live database source with a file source ---


def _mixed_connector():
    client = FakeClient()
    connector = JammiConnector(client, TABLES, database_sources={"wa_c1_orders"})  # type: ignore[arg-type]
    return connector, client


def test_query_mixing_database_and_file_sources_is_refused_before_running():
    from app.connectors.jammi_connector import CrossSourceQueryError

    connector, client = _mixed_connector()
    sql = ("SELECT c.customer_state, COUNT(*) AS n FROM wa_c1_orders.public.orders AS o "
           "JOIN wa_c1_customers.public.customers AS c ON o.customer_id = c.customer_id GROUP BY 1")
    with pytest.raises(CrossSourceQueryError, match="J28"):
        connector.execute_query(sql)
    assert not any(s.startswith("SELECT c.customer_state") for s in client.statements)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM wa_c1_orders.public.orders",  # database only
    "SELECT COUNT(*) AS n FROM wa_c1_customers.public.customers",  # file only
])
def test_single_kind_queries_still_run(sql):
    connector, client = _mixed_connector()
    connector.execute_query(sql)
    assert client.statements[-1].startswith(sql)


def test_database_sources_are_derived_from_the_naming_scheme():
    from app.services.jammi_sources import database_source_ids, source_id_for

    pg = source_id_for("u1", "c1", "pg")
    tables = {"orders": f"{pg}.public.orders", "regions": f"{source_id_for('u1', 'c1', 'regions')}.public.regions"}
    assert database_source_ids("u1", "c1", tables) == {pg}
    assert database_source_ids(None, "c1", tables) == set()


def test_chat_explains_a_refused_cross_source_question():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.chat import router
    from app.connectors.jammi_connector import CrossSourceQueryError
    from app.dependencies import get_query_service
    from app.security.auth import AuthUser, get_current_user

    class RefusingService:
        def run_question(self, *args, **kwargs):
            raise CrossSourceQueryError("CROSS_SOURCE_UNSAFE: ask about the database or the file separately")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_query_service] = lambda: RefusingService()
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="u1", email=None)
    response = TestClient(app).post("/chat", json={"question": "revenue by region"})
    assert response.status_code == 400 and response.json()["detail"].startswith("CROSS_SOURCE_UNSAFE")


class _NoApproxClient(FakeClient):
    """A live Postgres source: Jammi pushes approx_distinct down and Postgres has no such function (J31)."""

    def sql(self, query: str) -> pa.Table:
        if "approx_distinct" in query:
            self.statements.append(query)
            raise RuntimeError("ERROR: function approx_distinct(character varying) does not exist")
        return super().sql(query)


def test_distinct_counts_fall_back_to_exact_counts_where_approx_distinct_fails():
    client = _NoApproxClient()
    ctx = JammiConnector(client, TABLES).get_schema()  # type: ignore[arg-type]
    assert all(n > 0 for counts in ctx.distinct_counts.values() for n in counts.values())
    assert any("COUNT(DISTINCT" in s for s in client.statements)


def test_declared_joins_win_over_name_inference():
    declared = [{"from": "orders", "to": "customers", "type": "LEFT",
                 "on": "orders.customer_id = customers.customer_id"},
                {"from": "orders", "to": "elsewhere", "type": "LEFT", "on": "orders.x = elsewhere.x"}]
    ctx = JammiConnector(FakeClient(), TABLES, declared_joins=declared).get_schema()  # type: ignore[arg-type]
    assert ctx.join_paths == declared[:1]  # a table this connection does not have is dropped
    assert not any({j["from"], j["to"]} == {"orders", "customers"} for j in ctx.inferred_joins)


def test_hidden_columns_are_never_described_to_schema_generation():
    """J36: a column the live source cannot return (pre-1970 timestamps) must not reach the LLM."""
    ctx = JammiConnector(FakeClient(), TABLES, hidden_columns=["orders.freight"]).get_schema()  # type: ignore[arg-type]
    assert "freight" not in {c["name"] for c in ctx.tables["orders"]}
    assert "freight" not in ctx.distinct_counts["orders"] and "order_id" in ctx.distinct_counts["orders"]
    assert ctx.tables["customers"]  # other tables untouched
