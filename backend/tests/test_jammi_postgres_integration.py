"""Live tests: a real Postgres database registered through NLviz's routes into a real jammi-server.

Skipped unless both are configured. The Postgres must be reachable at the SAME address from this
process (table discovery) and from jammi-server (registration), as it is in Compose and on AWS:

    JAMMI_TEST_URL=grpc://127.0.0.1:18081
    JAMMI_TEST_PG_HOST=127.0.0.1 JAMMI_TEST_PG_PORT=15432 JAMMI_TEST_PG_DB=olist
    JAMMI_TEST_PG_USER=postgres JAMMI_TEST_PG_PASSWORD=...
    NLVIZ_TEST_DATABASE_URL=postgresql://postgres:...@127.0.0.1:15432/nlviz_app   (RDS store test)

The Olist tables are loaded with scripts/dev/load_olist_postgres.py.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.jammi

JAMMI_URL = os.getenv("JAMMI_TEST_URL")
PG = {
    "host": os.getenv("JAMMI_TEST_PG_HOST"),
    "port": int(os.getenv("JAMMI_TEST_PG_PORT", "5432")),
    "dbname": os.getenv("JAMMI_TEST_PG_DB", "olist"),
    "user": os.getenv("JAMMI_TEST_PG_USER", "postgres"),
    "password": os.getenv("JAMMI_TEST_PG_PASSWORD"),
    "schema": "public",
}
APP_DATABASE_URL = os.getenv("NLVIZ_TEST_DATABASE_URL")

if not (JAMMI_URL and PG["host"] and PG["password"]):
    pytest.skip("JAMMI_TEST_URL / JAMMI_TEST_PG_* not set; live Postgres+Jammi tests skipped",
                allow_module_level=True)

from app.dependencies import get_connection_service, get_jammi_provisioner  # noqa: E402
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.models.connection import ConnectionProfile  # noqa: E402
from app.services.connection_service import ConnectionService  # noqa: E402
from app.services.connection_store import ConnectionStore  # noqa: E402
from app.services.intent_mapper import IntentMapperConfig  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.secret_store import SecretStore  # noqa: E402

from golden_tests import GOLDEN_CASES  # noqa: E402
from test_connections_route import _build_connection_app  # noqa: E402
from test_jammi_integration import _rows_match, _service  # noqa: E402


@pytest.fixture(scope="module")
def jammi_client():
    return JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))


@pytest.fixture(scope="module")
def app(tmp_path_factory, jammi_client):
    tmp_path = tmp_path_factory.mktemp("pgapp")
    client, store, secrets = _build_connection_app(tmp_path)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi_client)
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi_client)
    client.app.dependency_overrides[get_connection_service] = lambda: service
    return client, store, service


@pytest.fixture(scope="module")
def pg_connection(app, jammi_client):
    client, store, _ = app
    response = client.post("/connections", json={"connector_type": "postgres", "params": PG,
                                                  "display_name": "Olist on Postgres"})
    assert response.status_code == 200, response.text
    profile = store.get_connection(response.json()["connection_id"])
    yield profile
    if store.get_connection(profile.connection_id).status == "active":
        client.delete(f"/connections/{profile.connection_id}")


def test_route_registers_the_database_as_one_jammi_source(pg_connection, jammi_client):
    assert pg_connection.engine == "jammi"
    assert set(pg_connection.jammi_tables) == {
        "category_translation", "customers", "order_items", "order_payments", "order_reviews",
        "orders", "products", "sellers",
    }
    source_ids = {p.split(".")[0] for p in pg_connection.jammi_tables.values()}
    assert len(source_ids) == 1 and source_ids <= jammi_client.source_ids()


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.question)
def test_golden_questions_on_federated_postgres_match_duckdb(case, app, pg_connection):
    from app.connectors import DuckDBConnector

    _, _, service = app
    connector = service.build_connector(pg_connection)
    reference = _service(DuckDBConnector()).run_question(case.question)
    ours = _service(connector, table_ref=connector.table_ref_resolver()).run_question(case.question)

    assert ours.intent == reference.intent
    assert ours.validation_status == reference.validation_status
    assert _rows_match(ours.rows, reference.rows), (ours.rows[:3], reference.rows[:3])


def test_non_public_schema_is_not_reachable_through_jammi(pg_connection, jammi_client):
    """Issue J24, live: the table exists in Postgres but Jammi only exposes 'public'."""
    source_id = next(iter(pg_connection.jammi_tables.values())).split(".")[0]
    assert jammi_client.sql(f"SELECT COUNT(*) AS n FROM {source_id}.public.orders").num_rows == 1
    with pytest.raises(Exception, match="not found"):
        jammi_client.sql(f"SELECT COUNT(*) AS n FROM {source_id}.analytics.daily_orders")


def test_views_in_public_are_not_reachable_through_jammi(jammi_client):
    """Issue J24, live: discovery lists base tables only, so even a view in 'public' is invisible."""
    import psycopg2

    from app.services.jammi_sources import postgres_url

    conn = psycopg2.connect(**{k: v for k, v in PG.items() if k != "schema"})
    conn.autocommit = True
    source_id = f"probe_views_{uuid.uuid4().hex[:8]}"
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE OR REPLACE VIEW public.v_probe_orders AS SELECT order_id FROM public.orders")
        jammi_client.add_source(source_id, url=postgres_url(PG))
        assert jammi_client.sql(f"SELECT COUNT(*) AS n FROM {source_id}.public.orders").num_rows == 1
        with pytest.raises(Exception, match="not found"):
            jammi_client.sql(f"SELECT COUNT(*) AS n FROM {source_id}.public.v_probe_orders")
    finally:
        if source_id in jammi_client.source_ids():
            jammi_client.remove_source(source_id)
        with conn.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS public.v_probe_orders")
        conn.close()


def test_route_refuses_a_non_public_schema(app):
    client, _, _ = app
    response = client.post("/connections", json={"connector_type": "postgres",
                                                  "params": {**PG, "schema": "analytics"},
                                                  "display_name": "Analytics"})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("POSTGRES_SCHEMA_UNSUPPORTED")


def test_wrong_password_is_refused_without_leaking_it(app, jammi_client):
    client, _, _ = app
    before = jammi_client.source_ids()
    bad = {**PG, "password": "wr0ng-Pa55/word"}
    response = client.post("/connections", json={"connector_type": "postgres", "params": bad,
                                                  "display_name": "Bad"})
    assert response.status_code in (400, 500)
    assert "wr0ng-Pa55" not in response.text and "wr0ng-Pa55%2Fword" not in response.text
    assert jammi_client.source_ids() == before


def test_delete_route_removes_the_jammi_source(app, jammi_client):
    client, store, _ = app
    response = client.post("/connections", json={"connector_type": "postgres", "params": PG,
                                                  "display_name": "To delete"})
    profile = store.get_connection(response.json()["connection_id"])
    source_id = next(iter(profile.jammi_tables.values())).split(".")[0]
    assert source_id in jammi_client.source_ids()

    assert client.delete(f"/connections/{profile.connection_id}").status_code == 200
    assert source_id not in jammi_client.source_ids()


@pytest.mark.skipif(not APP_DATABASE_URL, reason="NLVIZ_TEST_DATABASE_URL not set")
def test_rds_store_round_trips_jammi_profiles_and_denied_columns():
    """Migration 003 + the JSONB fix: non-empty denied_columns used to fail with DatatypeMismatch."""
    from app.services.connection_store import RDSConnectionStore
    from app.storage.db_pool import DatabasePool
    from migrations.migrate import run_migrations

    pool = DatabasePool(APP_DATABASE_URL)
    try:
        run_migrations(pool)
        store = RDSConnectionStore(pool)
        profile = ConnectionProfile(
            connection_id=str(uuid.uuid4()), display_name="pg", connector_type="postgres",
            created_at=datetime.now(timezone.utc), owner_id="user-1", denied_columns=["customer_unique_id"],
            engine="jammi", workspace_id="user-1",
            jammi_tables={"orders": "wabc_c1_pg.public.orders", "customers": "wabc_c1_pg.public.customers"},
        )
        store.create_connection(profile)
        assert store.get_connection(profile.connection_id) == profile
        legacy = profile.model_copy(update={"connection_id": str(uuid.uuid4()), "engine": "legacy",
                                            "workspace_id": None, "jammi_tables": {}, "denied_columns": []})
        store.create_connection(legacy)
        assert store.get_connection(legacy.connection_id) == legacy

        # GET /connections lists published ids; the pool returns dict rows, and positional access
        # (row[0]) failed with KeyError as soon as any schema was published (v1 bug, fixed in 3c).
        from app.models.connection import SemanticSchemaVersion

        store.create_version(SemanticSchemaVersion(
            version_id=str(uuid.uuid4()), connection_id=profile.connection_id, status="published",
            created_at=datetime.now(timezone.utc), schema_path="schemas/x.yaml",
        ))
        assert profile.connection_id in store.get_published_connection_ids()
    finally:
        pool.close()


def test_foreign_keys_are_read_for_a_read_only_user():
    """information_schema lists constraints only on tables the user owns: as a read-only user (the
    usual way to connect) Chinook showed none of its 11 foreign keys. pg_catalog shows them."""
    import psycopg2

    from app.api.routes.connections import _postgres_foreign_keys

    suffix = uuid.uuid4().hex[:8]
    role, password = f"nlviz_ro_{suffix}", f"pw-{uuid.uuid4().hex}"
    parent, child = f"fk_parent_{suffix}", f"fk_child_{suffix}"
    conn = psycopg2.connect(**{k: v for k, v in PG.items() if k != "schema"})
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE public.{parent} (id INT PRIMARY KEY, region TEXT)")
            cur.execute(f"CREATE TABLE public.{child} (id INT PRIMARY KEY, parent_ref INT REFERENCES public.{parent}(id))")
            cur.execute(f"CREATE ROLE {role} LOGIN PASSWORD %s", (password,))
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            cur.execute(f"GRANT SELECT ON public.{parent}, public.{child} TO {role}")
        joins = _postgres_foreign_keys({**PG, "user": role, "password": password}, {parent, child})
        assert joins == [{"from": child, "to": parent, "type": "LEFT", "relationship": "many_to_one",
                          "on": f"{child}.parent_ref = {parent}.id"}]
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS public.{child}, public.{parent}")
            cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone():
                cur.execute(f"REVOKE ALL ON SCHEMA public FROM {role}")
                cur.execute(f"DROP ROLE {role}")
        conn.close()


def test_a_database_with_types_jammi_cannot_read_live_is_copied_with_correct_values(tmp_path, jammi_client):
    """jsonb fails a live source (J33) and money reads 100x too large (J34): the database is copied
    with NLviz's own driver, and 12.34 stays 12.34."""
    import shutil
    from pathlib import Path

    import psycopg2

    from app.dependencies import get_jammi_upload_store
    from app.services.jammi_uploads import LocalUploadStore

    upload_root = Path(__file__).resolve().parents[1] / "data" / "parquet" / "_live_pgsnap"
    db = f"nlviz_pgsnap_{uuid.uuid4().hex[:6]}"
    admin = {k: v for k, v in PG.items() if k not in ("schema", "dbname")}
    root = psycopg2.connect(dbname="postgres", **admin)
    root.autocommit = True
    root.cursor().execute(f"CREATE DATABASE {db}")
    client, store, secrets = _build_connection_app(tmp_path)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi_client)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: LocalUploadStore(upload_root, "/data/_live_pgsnap")
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi_client)
    client.app.dependency_overrides[get_connection_service] = lambda: service
    try:
        conn = psycopg2.connect(dbname=db, **admin)
        conn.autocommit = True
        conn.cursor().execute(
            "CREATE TABLE sales (id int PRIMARY KEY, amount money, meta jsonb, tags text[]); "
            "INSERT INTO sales VALUES (1, 12.34, '{\"k\": 1}', '{a,b}'), (2, 0.50, NULL, NULL)")
        conn.close()
        response = client.post("/connections", json={"connector_type": "postgres", "params": {**PG, "dbname": db},
                                                      "display_name": "Sales with jsonb"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "snapshot" and "sales.amount (money)" in body["notes"][0]
        profile = store.get_connection(body["connection_id"])
        connector = service.build_connector(profile)
        frame = connector.execute_query(f"SELECT id, amount, meta, tags FROM {connector.table_ref('sales')} ORDER BY id")
        first, second = frame.to_dict("records")
        assert float(first["amount"]) == 12.34 and float(second["amount"]) == 0.5
        assert first["meta"] == '{"k": 1}' and first["tags"] == "a, b"
        assert client.delete(f"/connections/{profile.connection_id}").status_code == 200
    finally:
        root.cursor().execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        root.close()
        shutil.rmtree(upload_root, ignore_errors=True)


def test_timestamps_before_1970_are_left_out_and_the_rest_stays_live(tmp_path, jammi_client):
    """A `timestamp` before 1970 fails every live query that returns the column (J36; Chinook's
    employee birth dates). The column is left out of what schema generation sees, the database stays
    live, and the other columns answer; the timestamptz column (read correctly) stays in."""
    import shutil
    from pathlib import Path

    import psycopg2

    from app.dependencies import get_jammi_upload_store
    from app.services.jammi_uploads import LocalUploadStore

    upload_root = Path(__file__).resolve().parents[1] / "data" / "parquet" / "_live_pgepoch"
    db = f"nlviz_pgepoch_{uuid.uuid4().hex[:6]}"
    admin = {k: v for k, v in PG.items() if k not in ("schema", "dbname")}
    root = psycopg2.connect(dbname="postgres", **admin)
    root.autocommit = True
    root.cursor().execute(f"CREATE DATABASE {db}")
    client, store, secrets = _build_connection_app(tmp_path)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi_client)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: LocalUploadStore(upload_root, "/data/_live_pgepoch")
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi_client)
    client.app.dependency_overrides[get_connection_service] = lambda: service
    try:
        conn = psycopg2.connect(dbname=db, **admin)
        conn.autocommit = True
        conn.cursor().execute(
            "CREATE TABLE employee (id int PRIMARY KEY, birth_date timestamp, hired timestamptz); "
            "INSERT INTO employee VALUES (1, '1958-12-08', '2002-08-14+00'), (2, '1973-08-29', '2003-10-17+00')")
        conn.close()
        response = client.post("/connections", json={"connector_type": "postgres", "params": {**PG, "dbname": db},
                                                      "display_name": "Staff"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "live" and "employee.birth_date" in body["notes"][0]
        profile = store.get_connection(body["connection_id"])
        connector = service.build_connector(profile)
        columns = {c["name"] for c in connector.get_schema().tables["employee"]}
        assert "birth_date" not in columns and {"id", "hired"} <= columns
        frame = connector.execute_query(f"SELECT COUNT(*) AS n, MIN(hired) AS first FROM {connector.table_ref('employee')}")
        assert int(frame["n"].iloc[0]) == 2
        assert client.delete(f"/connections/{profile.connection_id}").status_code == 200
    finally:
        root.cursor().execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        root.close()
        shutil.rmtree(upload_root, ignore_errors=True)
