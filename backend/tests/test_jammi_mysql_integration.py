"""Live tests: a real MySQL database registered through NLviz's routes into a real jammi-server.

Skipped unless both are configured. MySQL must be reachable at the SAME address from this process
(discovery, snapshots) and from jammi-server (federation):

    JAMMI_TEST_URL=grpc://127.0.0.1:18081
    JAMMI_TEST_MYSQL_HOST=127.0.0.1 JAMMI_TEST_MYSQL_PORT=13306 JAMMI_TEST_MYSQL_DB=olist
    JAMMI_TEST_MYSQL_USER=root JAMMI_TEST_MYSQL_PASSWORD=...

Snapshot mode (the default) writes Parquet under backend/data/parquet/_live_mysql, which the dev
container sees as /data/_live_mysql. Federation mode needs jammi-server to trust the MySQL server's
certificate (scripts/dev/mysql_tls_setup.sh). Tables: scripts/dev/load_olist_mysql.py.
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import shutil
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.jammi

JAMMI_URL = os.getenv("JAMMI_TEST_URL")
MYSQL = {
    "host": os.getenv("JAMMI_TEST_MYSQL_HOST"),
    "port": int(os.getenv("JAMMI_TEST_MYSQL_PORT", "3306")),
    "dbname": os.getenv("JAMMI_TEST_MYSQL_DB", "olist"),
    "user": os.getenv("JAMMI_TEST_MYSQL_USER", "root"),
    "password": os.getenv("JAMMI_TEST_MYSQL_PASSWORD"),
}

if not (JAMMI_URL and MYSQL["host"] and MYSQL["password"]):
    pytest.skip("JAMMI_TEST_URL / JAMMI_TEST_MYSQL_* not set; live MySQL+Jammi tests skipped",
                allow_module_level=True)

from app.dependencies import (  # noqa: E402
    MySQLSettings,
    get_connection_service,
    get_jammi_provisioner,
    get_jammi_upload_store,
    get_mysql_settings,
)
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.services.connection_service import ConnectionService  # noqa: E402
from app.services.intent_mapper import IntentMapperConfig  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.jammi_uploads import LocalUploadStore  # noqa: E402

from golden_tests import GOLDEN_CASES  # noqa: E402
from test_connections_route import _build_connection_app  # noqa: E402
from test_jammi_integration import _rows_match, _service  # noqa: E402

OLIST_TABLES = {"category_translation", "customers", "order_items", "order_payments", "order_reviews",
                "orders", "products", "sellers"}
UPLOAD_ROOT = Path(__file__).resolve().parents[1] / "data" / "parquet" / "_live_mysql"
SERVER_ROOT = "/data/_live_mysql"

# Issue J27: federated MySQL gets these wrong (7 errors on date_trunc, 3 silently wrong results).
J27_FEDERATION_FAILURES = {
    "Show delivery time by seller state",  # DATE - DATE is numeric in MySQL: silent
    # "bottom 7 / first 3 customers by revenue" were here (NULLS LAST dropped, silent). Since revenue
    # is based on payment rows (review B3) no customer has a NULL total, so those questions no longer
    # show the defect; test_nulls_last_survives_mysql_pushdown below pins it directly.
    "Show revenue over time", "Show orders by month", "Show seller state orders over time",
    "weekly revenue trend", "quarterly revenue trend", "yearly revenue trend", "daily orders trend",
}


@pytest.fixture(scope="module")
def jammi_client():
    return JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))


def _make_app(tmp_path: Path, jammi_client, *, federation: bool):
    client, store, secrets = _build_connection_app(tmp_path)
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi_client)
    uploads = LocalUploadStore(UPLOAD_ROOT, SERVER_ROOT)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi_client)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: uploads
    client.app.dependency_overrides[get_mysql_settings] = lambda: MySQLSettings(federation=federation)
    client.app.dependency_overrides[get_connection_service] = lambda: service
    return client, store, service


@pytest.fixture(scope="module")
def app(tmp_path_factory, jammi_client):
    yield _make_app(tmp_path_factory.mktemp("mysql_snapshot"), jammi_client, federation=False)
    shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)


@pytest.fixture(scope="module")
def federated_app(tmp_path_factory, jammi_client):
    return _make_app(tmp_path_factory.mktemp("mysql_federated"), jammi_client, federation=True)


def _create(client, store, display_name="Olist on MySQL", params=None):
    response = client.post("/connections", json={"connector_type": "mysql", "params": params or MYSQL,
                                                  "display_name": display_name})
    assert response.status_code == 200, response.text
    return store.get_connection(response.json()["connection_id"]), response.json()


@pytest.fixture(scope="module")
def snapshot_connection(app):
    client, store, _ = app
    profile, body = _create(client, store)
    yield profile, body
    if store.get_connection(profile.connection_id).status == "active":
        client.delete(f"/connections/{profile.connection_id}")


@pytest.fixture(scope="module")
def federated_connection(federated_app):
    client, store, _ = federated_app
    profile, body = _create(client, store)
    yield profile, body
    if store.get_connection(profile.connection_id).status == "active":
        client.delete(f"/connections/{profile.connection_id}")


def test_connection_test_lists_the_tables(app):
    client, _, _ = app
    response = client.post("/connections/test", json={"connector_type": "mysql", "params": MYSQL})
    body = response.json()
    assert body["success"], body
    assert OLIST_TABLES <= {t["name"] for t in body["tables"]}


# ── Snapshot (default) ───────────────────────────────────────────────


def test_snapshot_registers_one_parquet_source_per_table(snapshot_connection, jammi_client):
    profile, body = snapshot_connection
    assert profile.engine == "jammi" and profile.connector_type == "mysql"
    # Views are snapshotted too; a mixed-case table is copied under its snake_case name.
    assert set(profile.jammi_tables) == OLIST_TABLES | {"v_orders", "audit_log"}
    assert body["skipped_tables"] == [] and body["renamed_columns"]["AuditLog"] == "audit_log"
    source_ids = {p.split(".")[0] for p in profile.jammi_tables.values()}
    assert len(source_ids) == len(profile.jammi_tables) and source_ids <= jammi_client.source_ids()
    assert len(list(UPLOAD_ROOT.rglob("*.parquet"))) >= len(profile.jammi_tables)


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.question)
def test_golden_questions_on_mysql_snapshot_match_duckdb(case, app, snapshot_connection):
    from app.connectors import DuckDBConnector

    _, _, service = app
    connector = service.build_connector(snapshot_connection[0])
    reference = _service(DuckDBConnector()).run_question(case.question)
    ours = _service(connector, table_ref=connector.table_ref_resolver()).run_question(case.question)

    assert ours.intent == reference.intent
    assert ours.validation_status == reference.validation_status
    assert _rows_match(ours.rows, reference.rows), (ours.rows[:3], reference.rows[:3])


def test_snapshot_keeps_mysql_types(app, jammi_client):
    """Awkward MySQL types survive the snapshot: DECIMAL, zero dates, BIT, unsigned BIGINT, JSON,
    TIME, ENUM, and a mixed-case column name."""
    import pymysql

    client, store, service = app
    db = f"nlviz_types_{uuid.uuid4().hex[:6]}"
    root = pymysql.connect(host=MYSQL["host"], port=MYSQL["port"], user=MYSQL["user"],
                           password=MYSQL["password"], autocommit=True)
    try:
        with root.cursor() as cur:
            cur.execute(f"CREATE DATABASE {db}")
            cur.execute("SET SESSION sql_mode = ''")  # allow a zero date, as legacy schemas have
            cur.execute(f"CREATE TABLE {db}.items (id BIGINT UNSIGNED, price DECIMAL(10,2), flags BIT(8), "
                        "made DATETIME(6), day DATE, took TIME, kind ENUM('a','b'), meta JSON, `UnitCost` DOUBLE)")
            cur.execute(f"INSERT INTO {db}.items VALUES (18446744073709551615, 12.34, b'00000101', "
                        "'0000-00-00 00:00:00', '2024-02-29', '01:02:03', 'b', '{\"k\": 1}', 1.5), "
                        "(1, NULL, NULL, '2024-01-02 03:04:05.123456', NULL, NULL, NULL, NULL, NULL)")
        profile, body = _create(client, store, "Types", {**MYSQL, "dbname": db})
        assert body["renamed_columns"] == {"items.UnitCost": "unit_cost"}  # camelCase split
        connector = service.build_connector(profile)
        frame = connector.execute_query(f"SELECT * FROM {connector.table_ref('items')} ORDER BY made NULLS FIRST")
        first, second = frame.to_dict("records")
        assert first["id"] == 18446744073709551615 and first["price"] == decimal.Decimal("12.34")
        assert first["flags"] == 5
        assert str(first["made"]) in ("NaT", "None")  # the zero date becomes NULL
        assert str(first["day"]) == "2024-02-29" and first["took"] == "1:02:03" and first["kind"] == "b"
        assert first["meta"] == '{"k": 1}' and first["unit_cost"] == 1.5
        assert second["made"] == dt.datetime(2024, 1, 2, 3, 4, 5, 123456)
        client.delete(f"/connections/{profile.connection_id}")
    finally:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
        root.close()


def test_snapshot_over_the_row_limit_is_refused_and_leaves_nothing(tmp_path, jammi_client):
    client, store, _ = _make_app(tmp_path, jammi_client, federation=False)
    client.app.dependency_overrides[get_mysql_settings] = lambda: MySQLSettings(snapshot_max_rows=1000)
    before_sources = jammi_client.source_ids()
    before_files = set(UPLOAD_ROOT.rglob("*.parquet")) if UPLOAD_ROOT.exists() else set()

    response = client.post("/connections", json={"connector_type": "mysql", "params": MYSQL,
                                                  "display_name": "Too big"})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("SNAPSHOT_FAILED") and "1,000" in response.json()["detail"]
    assert jammi_client.source_ids() == before_sources
    assert (set(UPLOAD_ROOT.rglob("*.parquet")) if UPLOAD_ROOT.exists() else set()) == before_files


def test_wrong_password_is_refused_without_leaking_it(app, jammi_client):
    client, _, _ = app
    before = jammi_client.source_ids()
    bad = {**MYSQL, "password": "wr0ng-Pa55/word"}
    response = client.post("/connections", json={"connector_type": "mysql", "params": bad,
                                                  "display_name": "Bad"})
    assert response.status_code == 400
    assert response.json()["detail"] == "AUTH_FAILED: wrong username or password"
    assert "wr0ng-Pa55" not in response.text and "wr0ng-Pa55%2Fword" not in response.text
    assert jammi_client.source_ids() == before


def test_delete_removes_the_sources_and_the_snapshot_files(app, jammi_client):
    client, store, _ = app
    profile, _ = _create(client, store, "To delete")
    source_ids = {p.split(".")[0] for p in profile.jammi_tables.values()}
    files = set(UPLOAD_ROOT.rglob("*.parquet"))
    assert source_ids <= jammi_client.source_ids()

    assert client.delete(f"/connections/{profile.connection_id}").status_code == 200

    assert not (source_ids & jammi_client.source_ids())
    remaining = set(UPLOAD_ROOT.rglob("*.parquet"))
    assert len(files - remaining) == len(profile.jammi_tables)


# ── Live federation (MYSQL_FEDERATION=true) ──────────────────────────


def test_federation_registers_tables_and_views_as_one_jammi_source(federated_connection, jammi_client):
    profile, body = federated_connection
    assert set(profile.jammi_tables) == OLIST_TABLES | {"v_orders"}  # Jammi's MySQL source lists views
    assert body["skipped_tables"] == ["AuditLog"]
    source_ids = {p.split(".")[0] for p in profile.jammi_tables.values()}
    assert len(source_ids) == 1 and source_ids <= jammi_client.source_ids()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, marks=pytest.mark.xfail(strict=True, reason="Jammi issue J27"))
        if case.question in J27_FEDERATION_FAILURES else case
        for case in GOLDEN_CASES
    ],
    ids=lambda case: case.question,
)
def test_golden_questions_on_federated_mysql(case, federated_app, federated_connection):
    from app.connectors import DuckDBConnector

    _, _, service = federated_app
    connector = service.build_connector(federated_connection[0])
    reference = _service(DuckDBConnector()).run_question(case.question)
    ours = _service(connector, table_ref=connector.table_ref_resolver()).run_question(case.question)

    assert ours.intent == reference.intent
    assert ours.validation_status == reference.validation_status
    assert _rows_match(ours.rows, reference.rows), (ours.rows[:3], reference.rows[:3])


@pytest.mark.xfail(strict=True, reason="Jammi issue J27: NULLS LAST is dropped when the sort is pushed to MySQL")
def test_nulls_last_survives_mysql_pushdown(jammi_client):
    """Canceled orders have no delivery date: ascending NULLS LAST must start with a delivered one."""
    from app.services.jammi_sources import mysql_url

    source = f"nlviz_j27_{uuid.uuid4().hex[:8]}"
    jammi_client.add_source(source, url=mysql_url(MYSQL))
    try:
        first = jammi_client.sql(
            f"SELECT order_delivered_customer_date AS d FROM {source}.public.orders "
            "ORDER BY order_delivered_customer_date ASC NULLS LAST LIMIT 1").column("d")[0].as_py()
    finally:
        jammi_client.remove_source(source)
    assert first is not None
