"""Live: a file attached to a live Postgres connection, and Jammi issue J28.

The file becomes one more table of the connection. Questions on the database alone or on the file
alone run normally. A question that joins the two is refused: Jammi 0.54 drops WHERE filters on the
database side of such a join (J28), which the strict expected failure below pins against DuckDB.

Needs JAMMI_TEST_URL and JAMMI_TEST_PG_* (see test_jammi_postgres_integration.py).
"""
from __future__ import annotations

import os
import shutil
import uuid
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
}
if not (JAMMI_URL and PG["host"] and PG["password"]):
    pytest.skip("JAMMI_TEST_URL / JAMMI_TEST_PG_* not set; live cross-source tests skipped", allow_module_level=True)

import duckdb  # noqa: E402
import yaml  # noqa: E402

from app.dependencies import get_connection_service, get_jammi_provisioner, get_jammi_upload_store  # noqa: E402
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.models import SemanticIntent  # noqa: E402
from app.semantic import SemanticRegistry, build_sql_from_intent  # noqa: E402
from app.semantic.models import SemanticSchema  # noqa: E402
from app.services.connection_service import ConnectionService  # noqa: E402
from app.services.intent_mapper import IntentMapperConfig  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.jammi_uploads import LocalUploadStore  # noqa: E402

from test_connections_route import _build_connection_app  # noqa: E402
from test_jammi_integration import _rows_match  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
REGIONS_CSV = BACKEND / "data" / "demo" / "brazil_regions.csv"
OLIST = BACKEND / "data" / "parquet" / "olist"
UPLOAD_ROOT = BACKEND / "data" / "parquet" / "_live_cross"


def _registry_with_regions() -> SemanticRegistry:
    """The demo schema plus the attached file: a table, its join and a region dimension."""
    raw = yaml.safe_load((BACKEND / "app" / "semantic" / "schemas" / "ecommerce.yaml").read_text(encoding="utf-8"))
    raw["tables"].append({"name": "brazil_regions", "description": "Brazilian states and their regions"})
    raw["joins"].append({"from": "customers", "to": "brazil_regions",
                         "on": "customers.customer_state = brazil_regions.state", "type": "LEFT"})
    raw["dimensions"].append({"name": "customer_region", "display_name": "Customer Region",
                              "sql_expression": "r.region",
                              "required_tables": ["orders", "customers c", "brazil_regions r"],
                              "cardinality": "low"})
    return SemanticRegistry(SemanticSchema.model_validate(raw))


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    jammi = JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))
    client, store, secrets = _build_connection_app(tmp_path_factory.mktemp("cross"))
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi)
    uploads = LocalUploadStore(UPLOAD_ROOT, "/data/_live_cross")
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: uploads
    client.app.dependency_overrides[get_connection_service] = lambda: service
    yield client, store, service, jammi
    shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)


@pytest.fixture(scope="module")
def connection(app):
    client, store, _, _ = app
    response = client.post("/connections", json={"connector_type": "postgres", "params": PG,
                                                  "display_name": "Olist + regions"})
    assert response.status_code == 200, response.text
    connection_id = response.json()["connection_id"]
    attached = client.post(f"/connections/{connection_id}/files",
                           files={"file": ("brazil_regions.csv", REGIONS_CSV.read_bytes(), "text/csv")})
    assert attached.status_code == 200, attached.text
    yield store.get_connection(connection_id), attached.json()
    client.delete(f"/connections/{connection_id}")


def test_attached_file_becomes_a_table_of_the_connection(connection, app):
    profile, attached = connection
    assert attached["table"] == "brazil_regions" and attached["schema_regeneration_required"]
    assert attached["renamed_columns"] == {"State": "state", "State Name": "state_name", "Region": "region"}
    assert "brazil_regions" in profile.jammi_tables and "orders" in profile.jammi_tables
    sources = {p.split(".")[0] for p in profile.jammi_tables.values()}
    assert len(sources) == 2  # the Postgres database and the file


def _reference(intent, registry):
    reference = duckdb.connect()
    for parquet in OLIST.glob("*.parquet"):
        reference.execute(f"CREATE VIEW {parquet.stem} AS SELECT * FROM read_parquet('{parquet.as_posix()}')")
    reference.execute(f"CREATE VIEW brazil_regions AS SELECT \"State\" AS state, \"State Name\" AS state_name, "
                      f"\"Region\" AS region FROM read_csv_auto('{REGIONS_CSV.as_posix()}')")
    return reference.execute(build_sql_from_intent(intent, registry)).df()


def test_cross_source_question_is_refused(connection, app):
    from app.connectors.jammi_connector import CrossSourceQueryError

    _, _, service, _ = app
    connector = service.build_connector(connection[0])
    intent = SemanticIntent(metric="total_revenue", dimensions=["customer_region"], limit=10)
    sql = build_sql_from_intent(intent, _registry_with_regions(), table_ref=connector.table_ref_resolver())
    with pytest.raises(CrossSourceQueryError, match="J28"):
        connector.execute_query(sql)


def test_database_and_file_questions_still_work_on_the_same_connection(connection, app):
    _, _, service, _ = app
    connector = service.build_connector(connection[0])
    registry = _registry_with_regions()
    intent = SemanticIntent(metric="total_revenue", dimensions=["customer_state"], limit=30)
    ours = connector.execute_query(build_sql_from_intent(intent, registry, table_ref=connector.table_ref_resolver()))
    assert _rows_match(ours.to_dict("records"), _reference(intent, registry).to_dict("records"))
    regions = connector.execute_query(f"SELECT region, COUNT(*) AS n FROM {connector.table_ref('brazil_regions')} "
                                      "GROUP BY region ORDER BY region")
    assert dict(zip(regions["region"], regions["n"])) == {
        "Centro-Oeste": 4, "Nordeste": 9, "Norte": 7, "Sudeste": 4, "Sul": 3}


@pytest.mark.parametrize("metric", [
    # total_revenue filters out cancelled orders: the filter is dropped (J28).
    pytest.param("total_revenue", marks=pytest.mark.xfail(strict=True, reason="Jammi issue J28")),
    # order_count has no filter, so there is nothing to drop: the control case.
    "order_count",
])
def test_j28_revenue_by_region_matches_duckdb_when_run_unguarded(connection, app, metric):
    from app.connectors.jammi_connector import JammiConnector

    _, _, _, jammi = app
    profile, _ = connection
    unguarded = JammiConnector(jammi, profile.jammi_tables)  # no database_sources: the guard is off
    registry = _registry_with_regions()
    intent = SemanticIntent(metric=metric, dimensions=["customer_region"], limit=10)
    ours = unguarded.execute_query(build_sql_from_intent(intent, registry, table_ref=unguarded.table_ref_resolver()))
    assert _rows_match(ours.to_dict("records"), _reference(intent, registry).to_dict("records"))


def test_attaching_a_clashing_table_name_is_refused(connection, app):
    client, _, _, jammi = app
    profile, _ = connection
    before = jammi.source_ids()
    response = client.post(f"/connections/{profile.connection_id}/files",
                           files={"file": ("orders.csv", b"a,b\n1,2\n", "text/csv")})
    assert response.status_code == 409 and jammi.source_ids() == before


def test_delete_removes_both_sources_and_the_file(app):
    client, store, _, jammi = app
    connection_id = client.post("/connections", json={"connector_type": "postgres", "params": PG,
                                                      "display_name": "To delete"}).json()["connection_id"]
    client.post(f"/connections/{connection_id}/files",
                files={"file": ("brazil_regions.csv", REGIONS_CSV.read_bytes(), "text/csv")})
    sources = {p.split(".")[0] for p in store.get_connection(connection_id).jammi_tables.values()}
    assert len(sources) == 2 and sources <= jammi.source_ids()

    assert client.delete(f"/connections/{connection_id}").status_code == 200

    assert not (sources & jammi.source_ids())
    assert not any(UPLOAD_ROOT.rglob(f"*{connection_id.replace('-', '')[:12]}*/*.parquet"))
