"""MySQL connections through Jammi, offline: type mapping, snapshot (default) and federation modes."""
from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.api.routes import connections as routes
from app.dependencies import MySQLSettings, get_jammi_provisioner, get_jammi_upload_store, get_mysql_settings
from app.services.jammi_sources import JammiSourceProvisioner, mysql_url, source_id_for
from app.services.jammi_uploads import LocalUploadStore
from app.services.mysql_snapshot import MySQLColumn, SnapshotTooLarge, _converter, arrow_type_for

from test_connections_route import _TEST_USER, _build_connection_app
from test_jammi_connection_routes import FakeJammi

PARAMS = {"host": "db.internal", "port": 3306, "dbname": "shop", "user": "reader", "password": "s3cr3t/pw"}


@pytest.mark.parametrize("data_type,column_type,expected", [
    ("int", "int(11)", pa.int64()),
    ("bigint", "bigint unsigned", pa.uint64()),
    ("decimal", "decimal(10,2)", pa.decimal128(10, 2)),
    ("double", "double", pa.float64()),
    ("bit", "bit(8)", pa.int64()),
    ("date", "date", pa.date32()),
    ("datetime", "datetime(6)", pa.timestamp("us")),
    ("time", "time", pa.string()),
    ("json", "json", pa.string()),
    ("blob", "blob", pa.binary()),
])
def test_mysql_types_map_to_arrow(data_type, column_type, expected):
    precision, scale = (10, 2) if data_type == "decimal" else (None, None)
    assert arrow_type_for(MySQLColumn("c", data_type, column_type, precision, scale)) == expected


def test_converters_handle_values_pyarrow_rejects():
    assert _converter(pa.timestamp("us"))("0000-00-00 00:00:00") is None  # zero date
    assert _converter(pa.date32())("0000-00-00") is None
    assert _converter(pa.int64())(b"\x05") == 5  # BIT
    assert _converter(pa.string())(dt.timedelta(hours=1, seconds=3)) == "1:00:03"  # TIME
    assert _converter(pa.decimal128(10, 2))(1.5) == decimal.Decimal("1.5")


def test_mysql_url_percent_encodes_credentials():
    assert mysql_url(PARAMS) == "mysql://reader:s3cr3t%2Fpw@db.internal:3306/shop"


def _app(tmp_path: Path, jammi: FakeJammi, *, federation=False, uploads=True):
    client, store, secrets = _build_connection_app(tmp_path)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    store_ = LocalUploadStore(tmp_path / "uploads", "/uploads") if uploads else None
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: store_
    client.app.dependency_overrides[get_mysql_settings] = lambda: MySQLSettings(federation=federation,
                                                                                 snapshot_max_rows=100)
    return client, store, secrets


@pytest.fixture
def mysql_db(monkeypatch):
    """A fake database: discovery lists tables; the snapshot writes small Parquet files."""
    monkeypatch.setattr(routes, "_discover_mysql_tables", lambda params: ["orders", "customers", "AuditLog"])
    calls = {}

    def fake_snapshot(connect, database, tables, workdir, *, max_rows):
        from app.services.mysql_snapshot import TableSnapshot

        calls.update(database=database, tables=tables, max_rows=max_rows)
        snaps = []
        from app.services.mysql_snapshot import snapshot_table_name

        for table in tables:
            path = workdir / f"{snapshot_table_name(table)}.parquet"
            pq.write_table(pa.table({"id": [1, 2]}), path)
            snaps.append(TableSnapshot(snapshot_table_name(table), path, 2, {"Id": "id"} if table == "orders" else {}))
        return snaps

    monkeypatch.setattr(routes, "snapshot_tables", fake_snapshot)
    return calls


def _create(client):
    return client.post("/connections", json={"connector_type": "mysql", "params": PARAMS, "display_name": "Shop"})


def test_snapshot_mode_registers_one_parquet_source_per_table(tmp_path: Path, mysql_db):
    jammi = FakeJammi()
    client, store, secrets = _app(tmp_path, jammi)

    response = _create(client)

    assert response.status_code == 200, response.text
    body = response.json()
    # A mixed-case table is copied under its snake_case name, and the rename is reported.
    assert body["skipped_tables"] == [] and body["renamed_columns"] == {"orders.Id": "id", "AuditLog": "audit_log"}
    assert mysql_db == {"database": "shop", "tables": ["orders", "customers", "AuditLog"], "max_rows": 100}
    profile = store.get_connection(body["connection_id"])
    assert profile.engine == "jammi" and profile.connector_type == "mysql"
    sid = source_id_for(_TEST_USER.user_id, body["connection_id"], "orders")
    assert profile.jammi_tables["orders"] == f"{sid}.public.orders"
    assert set(jammi.sources) == {sid, source_id_for(_TEST_USER.user_id, body["connection_id"], "customers"),
                                  source_id_for(_TEST_USER.user_id, body["connection_id"], "audit_log")}
    assert all(url.startswith("/uploads/") for url in jammi.sources.values())  # never a mysql:// URL
    stored = secrets.get_by_connection_id(body["connection_id"])
    assert stored["password"] == "s3cr3t/pw" and sorted(stored["upload_urls"]) == sorted(jammi.sources.values())


def test_snapshot_registration_failure_removes_the_files(tmp_path: Path, mysql_db):
    jammi = FakeJammi(fail_add_with="InvalidArgument: boom")
    client, store, _ = _app(tmp_path, jammi)

    response = _create(client)

    assert response.status_code == 400 and response.json()["detail"].startswith("JAMMI_REGISTRATION_FAILED")
    assert not any((tmp_path / "uploads").rglob("*.parquet"))
    assert store.list_connections(owner_id=_TEST_USER.user_id) == []
    assert not any((tmp_path / ".secrets").glob("*"))


def test_snapshot_too_large_is_a_clear_400(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(routes, "_discover_mysql_tables", lambda params: ["orders"])

    def too_large(*args, **kwargs):
        raise SnapshotTooLarge("The database has more than 100 rows, the snapshot limit")

    monkeypatch.setattr(routes, "snapshot_tables", too_large)
    jammi = FakeJammi()
    client, store, _ = _app(tmp_path, jammi)

    response = _create(client)

    assert response.status_code == 400 and response.json()["detail"].startswith("SNAPSHOT_FAILED")
    assert jammi.sources == {} and not any((tmp_path / ".secrets").glob("*"))


def test_store_failure_removes_sources_and_files(tmp_path: Path, mysql_db, monkeypatch):
    jammi = FakeJammi()
    client, store, _ = _app(tmp_path, jammi)

    def broken_create(profile):
        raise RuntimeError("database down")

    monkeypatch.setattr(store, "create_connection", broken_create)
    with pytest.raises(RuntimeError, match="database down"):
        _create(client)

    assert jammi.sources == {} and len(jammi.removed) == 3  # orders, customers, audit_log
    assert not any((tmp_path / "uploads").rglob("*.parquet"))
    assert not any((tmp_path / ".secrets").glob("*"))


def test_federation_mode_registers_the_database_url(tmp_path: Path, mysql_db):
    jammi = FakeJammi()
    client, store, _ = _app(tmp_path, jammi, federation=True)

    body = _create(client).json()

    sid = source_id_for(_TEST_USER.user_id, body["connection_id"], "mysql")
    assert jammi.sources == {sid: "mysql://reader:s3cr3t%2Fpw@db.internal:3306/shop"}
    assert store.get_connection(body["connection_id"]).jammi_tables == {
        "orders": f"{sid}.public.orders", "customers": f"{sid}.public.customers"}
    assert mysql_db == {}  # no snapshot taken


@pytest.mark.parametrize("configure", [
    lambda client: client.app.dependency_overrides.pop(get_jammi_provisioner),  # no Jammi at all
    lambda client: client.app.dependency_overrides.__setitem__(get_jammi_upload_store, lambda: None),  # no storage
])
def test_mysql_without_jammi_or_storage_is_refused(tmp_path: Path, mysql_db, configure):
    client, store, _ = _app(tmp_path, FakeJammi())
    configure(client)

    response = _create(client)

    assert response.status_code == 400 and response.json()["detail"].startswith("MYSQL_REQUIRES_JAMMI")
    assert store.list_connections(owner_id=_TEST_USER.user_id) == []
    assert not any((tmp_path / ".secrets").glob("*"))


def test_snapshot_names_are_snake_case():
    from app.services.jammi_uploads import snake_identifier
    from app.services.mysql_snapshot import snapshot_table_name

    assert snapshot_table_name("InvoiceLine") == "invoice_line" and snapshot_table_name("orders") == "orders"
    for raw, expected in [("InvoiceLineId", "invoice_line_id"), ("MediaTypeId", "media_type_id"),
                          ("customer_id", "customer_id"), ("HTTPStatus", "http_status"), ("Id", "id")]:
        assert snake_identifier(raw, fallback="c", digit_prefix="c_") == expected
