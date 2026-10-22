"""Live: importing an object from the user's S3 bucket into a real jammi-server.

Skipped unless JAMMI_TEST_URL and NLVIZ_TEST_S3_ENDPOINT are set. The S3-compatible dev store is
SeaweedFS on 127.0.0.1:19000 with a private bucket (owner keys) and a public one (anonymous read),
seeded by scripts/dev/seed_s3_dev.py.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pyarrow.parquet as pq
import pytest

pytestmark = pytest.mark.jammi

JAMMI_URL = os.getenv("JAMMI_TEST_URL")
S3_ENDPOINT = os.getenv("NLVIZ_TEST_S3_ENDPOINT")
if not (JAMMI_URL and S3_ENDPOINT):
    pytest.skip("JAMMI_TEST_URL / NLVIZ_TEST_S3_ENDPOINT not set; live S3 import tests skipped",
                allow_module_level=True)

from app.api.routes import connections as routes  # noqa: E402
from app.dependencies import (  # noqa: E402
    get_connection_service,
    get_jammi_provisioner,
    get_jammi_upload_store,
    get_s3_import_allow_custom_endpoint,
)
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.services.connection_service import ConnectionService  # noqa: E402
from app.services.intent_mapper import IntentMapperConfig  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.jammi_uploads import LocalUploadStore  # noqa: E402

from test_connections_route import _build_connection_app  # noqa: E402

OWNER_KEY, OWNER_SECRET = "nlvizminio", "nlviz-minio-dev-pw"
UPLOAD_ROOT = Path(__file__).resolve().parents[1] / "data" / "parquet" / "_live_s3"
SELLERS = Path(__file__).resolve().parents[1] / "data" / "parquet" / "olist" / "sellers.parquet"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    jammi = JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))
    client, store, secrets = _build_connection_app(tmp_path_factory.mktemp("s3import"))
    uploads = LocalUploadStore(UPLOAD_ROOT, "/data/_live_s3")
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: uploads
    client.app.dependency_overrides[get_s3_import_allow_custom_endpoint] = lambda: True
    client.app.dependency_overrides[get_connection_service] = lambda: service
    yield client, store, service, secrets
    shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)


def _import(client, s3_url, **extra):
    return client.post("/connections/s3-import", json={"s3_url": s3_url, "display_name": "Imported",
                                                       "endpoint_url": S3_ENDPOINT, **extra})


def test_private_object_imports_with_the_users_keys(app):
    client, store, service, secrets = app

    response = _import(client, "s3://nlviz-private/exports/sellers.parquet",
                       access_key_id=OWNER_KEY, secret_access_key=OWNER_SECRET)

    assert response.status_code == 200, response.text
    connection_id = response.json()["connection_id"]
    profile = store.get_connection(connection_id)
    assert profile.connector_type == "upload" and set(profile.jammi_tables) == {"sellers"}
    connector = service.build_connector(profile)
    frame = connector.execute_query(f"SELECT COUNT(*) AS n FROM {connector.table_ref('sellers')}")
    assert frame.iloc[0]["n"] == pq.read_table(SELLERS).num_rows
    # The user's S3 credentials are used once and never stored.
    assert OWNER_SECRET not in str(secrets.get_by_connection_id(connection_id))


def test_csv_object_imports_with_renamed_columns(app):
    client, store, service, _ = app

    response = _import(client, "s3://nlviz-private/exports/sales.csv",
                       access_key_id=OWNER_KEY, secret_access_key=OWNER_SECRET)

    assert response.json()["renamed_columns"] == {"Region": "region", "Amount": "amount"}
    connector = service.build_connector(store.get_connection(response.json()["connection_id"]))
    frame = connector.execute_query(
        f"SELECT region, SUM(amount) AS total FROM {connector.table_ref('sales')} GROUP BY region ORDER BY region")
    assert frame.to_dict("records") == [{"region": "north", "total": 11}, {"region": "south", "total": 5}]


def test_public_object_imports_unsigned(app):
    client, _, _, _ = app
    response = _import(client, "s3://nlviz-public/category_translation.parquet")
    assert response.status_code == 200, response.text


def test_backend_credentials_are_never_used(app, monkeypatch):
    """Even with valid keys in NLviz's own environment, an unsigned import of a private object is
    refused: the client never consults the default credential chain (J25)."""
    client, _, _, _ = app
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", OWNER_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", OWNER_SECRET)

    response = _import(client, "s3://nlviz-private/exports/sellers.parquet")

    assert response.status_code == 400
    assert response.json()["detail"].startswith("S3_IMPORT_FAILED: Access denied")


def test_wrong_secret_is_refused_without_echoing_it(app):
    client, _, _, _ = app
    response = _import(client, "s3://nlviz-private/exports/sellers.parquet",
                       access_key_id=OWNER_KEY, secret_access_key="wr0ng-s3cret/value")
    assert response.status_code == 400 and response.json()["detail"].startswith("S3_IMPORT_FAILED")
    assert "wr0ng-s3cret" not in response.text


def test_object_over_the_size_limit_is_refused(app, monkeypatch):
    client, _, _, _ = app
    monkeypatch.setattr(routes, "_MAX_UPLOAD_MB", 0)
    response = _import(client, "s3://nlviz-public/category_translation.parquet")
    assert response.status_code == 400 and "limit" in response.json()["detail"]


def test_missing_object_is_a_clear_error(app):
    client, _, _, _ = app
    response = _import(client, "s3://nlviz-public/nope.parquet")
    assert response.status_code == 400
    assert response.json()["detail"] in ("S3_IMPORT_FAILED: Object not found",
                                         "S3_IMPORT_FAILED: Access denied: check the credentials, or whether the bucket is public")
