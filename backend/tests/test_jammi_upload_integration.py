"""Live: files uploaded through NLviz's route are served by a real jammi-server.

Skipped unless JAMMI_TEST_URL is set. The dev container mounts backend/data/parquet at /data, so the
test writes uploads under backend/data/parquet/_live_uploads (seen by the server as
/data/_live_uploads) and removes them afterwards.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

pytestmark = pytest.mark.jammi

JAMMI_URL = os.getenv("JAMMI_TEST_URL")
if not JAMMI_URL:
    pytest.skip("JAMMI_TEST_URL not set; live Jammi upload tests skipped", allow_module_level=True)

from app.dependencies import get_connection_service, get_jammi_provisioner, get_jammi_upload_store  # noqa: E402
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.services.connection_service import ConnectionService  # noqa: E402
from app.services.intent_mapper import IntentMapperConfig  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.jammi_uploads import LocalUploadStore  # noqa: E402

from test_connections_route import _build_connection_app  # noqa: E402

UPLOAD_ROOT = Path(__file__).resolve().parents[1] / "data" / "parquet" / "_live_uploads"
SERVER_ROOT = "/data/_live_uploads"
OLIST_ORDERS = Path(__file__).resolve().parents[1] / "data" / "parquet" / "olist" / "orders.parquet"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    jammi = JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))
    client, store, secrets = _build_connection_app(tmp_path_factory.mktemp("uploads"))
    uploads = LocalUploadStore(UPLOAD_ROOT, SERVER_ROOT)
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: uploads
    client.app.dependency_overrides[get_connection_service] = lambda: service
    yield client, store, service, jammi
    shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)


def _upload(client, name: str, payload: bytes, mime: str = "text/csv"):
    response = client.post("/connections/upload", files={"file": (name, payload, mime)},
                           data={"display_name": name})
    assert response.status_code == 200, response.text
    return response.json()


def test_csv_upload_is_queryable_through_jammi(app):
    client, store, service, jammi = app
    csv = "Order ID,City,Total (BRL)\n1,São Paulo,10.5\n2,Recife,20.25\n3,São Paulo,1.25\n".encode("cp1252")

    body = _upload(client, "Sales Q1.csv", csv)

    assert body["renamed_columns"] == {"Order ID": "order_id", "City": "city", "Total (BRL)": "total_brl"}
    profile = store.get_connection(body["connection_id"])
    connector = service.build_connector(profile)
    frame = connector.execute_query(
        "SELECT city, SUM(total_brl) AS total FROM sales_q1 GROUP BY city ORDER BY city"
        .replace("sales_q1", connector.table_ref("sales_q1"))
    )
    assert frame.to_dict("records") == [{"city": "Recife", "total": 20.25}, {"city": "São Paulo", "total": 11.75}]
    schema = connector.get_schema()
    assert {c["name"] for c in schema.tables["sales_q1"]} == {"order_id", "city", "total_brl"}
    assert schema.row_counts["sales_q1"] == 3


def test_parquet_upload_matches_the_source_file(app):
    client, store, service, jammi = app

    body = _upload(client, "orders.parquet", OLIST_ORDERS.read_bytes(), "application/octet-stream")

    connector = service.build_connector(store.get_connection(body["connection_id"]))
    frame = connector.execute_query(f"SELECT COUNT(*) AS n, COUNT(DISTINCT customer_id) AS c FROM "
                                    f"{connector.table_ref('orders')}")
    expected = pq.read_table(OLIST_ORDERS)
    assert frame.iloc[0]["n"] == expected.num_rows
    assert frame.iloc[0]["c"] == len(pa.compute.unique(expected.column("customer_id")))


def test_delete_removes_the_server_source_and_the_file(app):
    client, store, service, jammi = app
    body = _upload(client, "to delete.csv", b"a,b\n1,2\n")
    profile = store.get_connection(body["connection_id"])
    source_id = profile.jammi_tables["to_delete"].split(".")[0]
    assert source_id in jammi.source_ids()
    assert any(UPLOAD_ROOT.rglob("to_delete.parquet"))

    assert client.delete(f"/connections/{body['connection_id']}").status_code == 200

    assert source_id not in jammi.source_ids()
    assert not any(UPLOAD_ROOT.rglob("to_delete.parquet"))
