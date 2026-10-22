"""Live: the demo's value index built on a real jammi-server, and snapping against it.

Needs JAMMI_TEST_URL (the dev container mounts backend/data/parquet at /data).
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.jammi

JAMMI_URL = os.getenv("JAMMI_TEST_URL")
if not JAMMI_URL:
    pytest.skip("JAMMI_TEST_URL not set; live grounding tests skipped", allow_module_level=True)

from app.dependencies import build_demo_jammi_connector  # noqa: E402
from app.grounding.service import GroundingService  # noqa: E402
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.semantic import load_semantic_registry  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.jammi_uploads import LocalUploadStore  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
UPLOAD_ROOT = BACKEND / "data" / "parquet" / "_live_grounding"
REGISTRY = load_semantic_registry(BACKEND / "app" / "semantic" / "schemas" / "ecommerce.yaml")
# Source ids are global while listing is per tenant (J14): register the demo under a workspace of
# this run's own, and remove it afterwards, so runs never collide with each other's sources.
WORKSPACE = f"grounding-test-{uuid.uuid4().hex[:8]}"


def _demo(client):
    return build_demo_jammi_connector(client, "/data/olist", [t.name for t in REGISTRY.schema.tables],
                                      workspace_id=WORKSPACE)


@pytest.fixture(scope="module")
def grounding():
    client = JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))
    connector = _demo(client)
    service = GroundingService(client, LocalUploadStore(UPLOAD_ROOT, "/data/_live_grounding"))
    connection_id = f"test{uuid.uuid4().hex[:8]}"
    index = service.ensure(connection_id=connection_id, workspace_id="grounding-test",
                           connector=connector, registry=REGISTRY).result(timeout=600)
    assert index is not None
    yield service, index, client, connection_id
    service.remove(connection_id=connection_id, workspace_id="grounding-test")
    service.shutdown()
    JammiSourceProvisioner(client).deprovision(connector.tables)
    shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)


def test_index_covers_the_low_and_medium_cardinality_dimensions(grounding):
    _, index, client, _ = grounding
    assert set(index.dimensions) == {"product_category", "customer_state", "seller_state", "payment_type",
                                     "order_status", "review_score"}
    assert set(index.sources) <= client.source_ids()


@pytest.mark.parametrize("dimension,raw,expected", [
    ("customer_state", "São Paulo", "SP"),            # synonym
    ("customer_state", "rio grande do sul", "RS"),    # synonym, folded
    ("payment_type", "bank slip", "boleto"),          # synonym
    ("order_status", "cancelled", "canceled"),        # spelling variant via synonym
    ("product_category", "health beauty", "health_beauty"),     # folded exact
    ("product_category", "computer accessories", "computers_accessories"),  # near miss → retrieval
    ("payment_type", "credit", "credit_card"),        # partial → retrieval
])
def test_snapping_real_values(grounding, dimension, raw, expected):
    _, index, _, _ = grounding
    result = index.snap(dimension, raw)
    assert result.value == expected or expected in result.alternatives, result


def test_candidates_surface_values_mentioned_in_a_question(grounding):
    _, index, _, _ = grounding
    found = {(m.dimension, m.value) for m in index.candidates("revenue from Rio de Janeiro paid with credit card")}
    assert ("customer_state", "RJ") in found and ("payment_type", "credit_card") in found


def test_rebuild_with_unchanged_values_reuses_the_index(grounding):
    service, index, _, connection_id = grounding
    again = service.ensure(connection_id=f"{connection_id}", workspace_id="grounding-test",
                           connector=_demo(grounding[2]),
                           registry=REGISTRY).result(timeout=120)
    assert again.sources == index.sources


def test_remove_drops_the_index_sources(grounding):
    service, _, client, _ = grounding
    connection_id = f"rm{uuid.uuid4().hex[:8]}"
    connector = _demo(client)
    built = service.ensure(connection_id=connection_id, workspace_id="grounding-test",
                           connector=connector, registry=REGISTRY).result(timeout=600)
    assert set(built.sources) <= client.source_ids()
    service.remove(connection_id=connection_id, workspace_id="grounding-test")
    assert not set(built.sources) & client.source_ids()
