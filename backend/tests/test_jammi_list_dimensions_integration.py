"""Live (Phase 4): list-valued columns and placeholder zeros on a real jammi-server, through the safety gate.

Skipped unless JAMMI_TEST_URL is set. Same upload layout as test_jammi_upload_integration.py.
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
    pytest.skip("JAMMI_TEST_URL not set; live Jammi list-dimension tests skipped", allow_module_level=True)

from app.dependencies import get_connection_service, get_jammi_provisioner, get_jammi_upload_store  # noqa: E402
from app.jammi.client import JammiClient, JammiSettings  # noqa: E402
from app.models import SemanticIntent  # noqa: E402
from app.models.semantic_intent import FilterCondition  # noqa: E402
from app.semantic import SemanticRegistry, build_sql_from_intent  # noqa: E402
from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable  # noqa: E402
from app.services.connection_service import ConnectionService  # noqa: E402
from app.services.intent_mapper import IntentMapperConfig  # noqa: E402
from app.services.jammi_sources import JammiSourceProvisioner  # noqa: E402
from app.services.jammi_uploads import LocalUploadStore  # noqa: E402
from app.services.schema_generator import detect_list_dimensions, exclude_placeholder_zeros  # noqa: E402

from test_connections_route import _build_connection_app  # noqa: E402

UPLOAD_ROOT = Path(__file__).resolve().parents[1] / "data" / "parquet" / "_live_lists"
SERVER_ROOT = "/data/_live_lists"

# 24 restaurants; cuisines are lists over 6 items with irregular spacing; rating 0 = not rated.
ROWS = [
    (1, "A", "Chinese, Thai", 4.1, 10), (2, "A", "Chinese", 3.9, 5), (3, "A", "Thai,Japanese", 0.0, 0),
    (4, "B", "Pizza , Cafe", 4.5, 50), (5, "B", "Cafe", 3.2, 7), (6, "B", "Chinese,  Pizza", 0.0, 0),
    (7, "C", "Japanese, Sushi", 4.8, 90), (8, "C", "Sushi", 4.0, 12), (9, "C", "Thai, Chinese, Cafe", 3.5, 3),
    (10, "A", "Pizza", 2.9, 4), (11, "B", "Japanese", 3.3, 6), (12, "C", "Chinese, Japanese", 4.2, 20),
    (13, "A", "Cafe, Pizza", 3.7, 8), (14, "B", "Thai, Pizza", 0.0, 0), (15, "C", "Sushi, Cafe", 4.4, 30),
    (16, "A", "Chinese, Sushi", 3.6, 9), (17, "B", "Japanese, Thai", 4.0, 11), (18, "C", "Pizza, Chinese", 3.1, 2),
    (19, "A", "Thai, Cafe", 3.8, 14), (20, "B", "Sushi, Pizza", 4.6, 40), (21, "C", "Cafe, Japanese", 3.4, 5),
    (22, "A", "Japanese, Pizza", 0.0, 0), (23, "B", "Chinese, Cafe", 3.9, 13), (24, "C", "Thai, Sushi", 4.3, 25),
]


def _items(text: str) -> list[str]:
    return [part.strip() for part in text.split(",")]


@pytest.fixture(scope="module")
def connector(tmp_path_factory):
    jammi = JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))
    client, store, secrets = _build_connection_app(tmp_path_factory.mktemp("lists"))
    service = ConnectionService(store, secrets, IntentMapperConfig(mode="heuristic"), jammi_client=jammi)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(jammi)
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: LocalUploadStore(UPLOAD_ROOT, SERVER_ROOT)
    client.app.dependency_overrides[get_connection_service] = lambda: service
    csv = "id,city,cuisines,rating,votes\n" + "".join(f'{i},{c},"{k}",{r},{v}\n' for i, c, k, r, v in ROWS)
    response = client.post("/connections/upload", files={"file": ("places.csv", csv.encode(), "text/csv")},
                           data={"display_name": "places"})
    assert response.status_code == 200, response.text
    profile = store.get_connection(response.json()["connection_id"])
    yield service.build_connector(profile)
    client.delete(f"/connections/{profile.connection_id}")
    shutil.rmtree(UPLOAD_ROOT, ignore_errors=True)


def _schema() -> SemanticSchema:
    return SemanticSchema(
        version="1.0", dataset="places", description="Places", tables=[SemanticTable(name="places")], joins=[],
        metrics=[SemanticMetric(name="places_n", display_name="Places", description="Places",
                                aggregation="COUNT_DISTINCT", sql_expression="places.id", required_tables=["places"]),
                 SemanticMetric(name="avg_rating", display_name="Average Rating", description="Average Rating",
                                aggregation="AVG", sql_expression="places.rating", required_tables=["places"]),
                 SemanticMetric(name="votes", display_name="Votes", description="Votes",
                                aggregation="SUM", sql_expression="places.votes", required_tables=["places"])],
        dimensions=[SemanticDimension(name="cuisines", display_name="Cuisines", sql_expression="places.cuisines",
                                      required_tables=["places"]),
                    SemanticDimension(name="city", display_name="City", sql_expression="places.city",
                                      required_tables=["places"])],
        time_dimensions=[])


def _run(connector, registry, intent) -> list[dict]:
    sql = build_sql_from_intent(intent, registry, table_ref=connector.table_ref_resolver())
    return connector.execute_query(sql).to_dict("records")


def test_profiling_marks_the_list_column_and_skips_placeholder_zeros(connector):
    ctx = connector.get_schema()
    schema = exclude_placeholder_zeros(detect_list_dimensions(_schema(), ctx, connector), connector)
    dims = {d.name: d for d in schema.dimensions}
    assert dims["cuisines"].separator == "," and dims["cuisines"].cardinality == "low"
    assert dims["city"].separator is None
    by_name = {m.name: m for m in schema.metrics}
    # 5 of 24 zeros, real 2.9-4.8: a variant without zeros is proposed; the average itself is unchanged.
    assert by_name["avg_rating"].sql_expression == "places.rating"
    assert by_name["avg_rating_excluding_zero"].sql_expression == "NULLIF(places.rating, 0)"
    assert by_name["votes"].sql_expression == "places.votes"                  # SUM is never changed


def test_item_filters_and_per_item_breakdowns_match_python(connector):
    schema = _schema()
    schema.dimensions[0] = schema.dimensions[0].model_copy(update={"separator": ","})
    registry = SemanticRegistry(schema)

    chinese = _run(connector, registry, SemanticIntent(metric="places_n", filters=[
        FilterCondition(dimension="cuisines", operator="eq", value="Chinese")]))
    assert chinese == [{"metric_value": sum("Chinese" in _items(k) for _, _, k, _, _ in ROWS)}]

    either = _run(connector, registry, SemanticIntent(metric="places_n", filters=[
        FilterCondition(dimension="cuisines", operator="in", value=["Sushi", "Cafe"])]))
    assert either == [{"metric_value": sum(bool({"Sushi", "Cafe"} & set(_items(k))) for _, _, k, _, _ in ROWS)}]

    not_thai = _run(connector, registry, SemanticIntent(metric="places_n", filters=[
        FilterCondition(dimension="cuisines", operator="ne", value="Thai")]))
    assert not_thai == [{"metric_value": sum("Thai" not in _items(k) for _, _, k, _, _ in ROWS)}]

    per_item = _run(connector, registry, SemanticIntent(metric="votes", dimensions=["cuisines"], order_by="metric_desc"))
    expected: dict[str, int] = {}
    for _, _, k, _, v in ROWS:
        for item in _items(k):
            expected[item] = expected.get(item, 0) + v
    assert {r["cuisines"]: r["metric_value"] for r in per_item} == expected

    # Two dimensions, one a list, with a filter: grouped by the expression, not the raw column.
    by_city = _run(connector, registry, SemanticIntent(metric="places_n", dimensions=["city", "cuisines"], filters=[
        FilterCondition(dimension="city", operator="eq", value="A")]))
    expected_a: dict[str, int] = {}
    for _, c, k, _, _ in ROWS:
        if c == "A":
            for item in _items(k):
                expected_a[item] = expected_a.get(item, 0) + 1
    assert {r["cuisines"]: r["metric_value"] for r in by_city} == expected_a
    assert {r["city"] for r in by_city} == {"A"}
