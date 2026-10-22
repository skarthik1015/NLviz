"""Integration tests against a live jammi-server (skipped unless JAMMI_TEST_URL is set).

    docker run -d --name nlviz-jammi-dev -p 127.0.0.1:18081:8081 -v <backend>/data/parquet:/data:ro \
        ghcr.io/f-inverse/jammi-ai-server:0.54.0
    JAMMI_TEST_URL=grpc://127.0.0.1:18081 pytest tests/test_jammi_integration.py

JAMMI_TEST_DATA_DIR is the Olist Parquet directory as the SERVER sees it (default /data/olist).
Every run registers sources under a fresh service tenant and unique names, then removes them.
"""
from __future__ import annotations

import math
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.connectors import DuckDBConnector
from app.connectors.jammi_connector import JammiConnector
from app.jammi.client import JammiClient, JammiSettings
from app.security import SQLSafetyError
from app.semantic import load_semantic_registry
from app.services import HeuristicIntentMapper, IntentMapperConfig, IntentMapperRouter, QueryService

from golden_tests import GOLDEN_CASES  # tests/ is on sys.path under pytest

pytestmark = pytest.mark.jammi

JAMMI_URL = os.getenv("JAMMI_TEST_URL")
DATA_DIR = os.getenv("JAMMI_TEST_DATA_DIR", "/data/olist")
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
TABLES = ["category_translation", "customers", "order_items", "order_payments", "order_reviews",
          "orders", "products", "sellers"]

if not JAMMI_URL:
    pytest.skip("JAMMI_TEST_URL not set; live jammi-server tests skipped", allow_module_level=True)


@pytest.fixture(scope="module")
def jammi_client():
    return JammiClient(JammiSettings(url=JAMMI_URL, service_tenant=str(uuid.uuid4())))


@pytest.fixture(scope="module")
def workspace(jammi_client):
    """Registers the Olist tables as one workspace connection; removes them afterwards."""
    run = uuid.uuid4().hex[:8]
    mapping = {}
    for table in TABLES:
        source_id = f"wtest{run}_c1_{table}"
        jammi_client.add_source(source_id, url=f"{DATA_DIR}/{table}.parquet", format="parquet")
        mapping[table] = f"{source_id}.public.{table}"
    yield mapping
    for physical in mapping.values():
        jammi_client.remove_source(physical.split(".")[0])


@pytest.fixture(scope="module")
def jammi_connector(jammi_client, workspace):
    return JammiConnector(jammi_client, workspace)


def _service(connector, table_ref=None) -> QueryService:
    config = IntentMapperConfig(mode="heuristic")
    return QueryService(
        QueryGraphRunner(
            QueryGraphDependencies(
                connector=connector,
                schema_context=connector.get_schema(),
                registry=load_semantic_registry(SCHEMA_PATH),
                intent_mapper=IntentMapperRouter(config=config, heuristic_mapper=HeuristicIntentMapper()),
                intent_config=config,
                table_ref=table_ref,
            )
        )
    )


def _norm(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).isoformat()
    if hasattr(value, "item"):  # numpy scalars
        value = value.item()
    return value


def _rows_match(got, expected) -> bool:
    """Equal row by row; floats within a relative 1e-9 (summation order differs per engine)."""
    if len(got) != len(expected):
        return False
    for got_row, expected_row in zip(got, expected):
        if got_row.keys() != expected_row.keys():
            return False
        for key in got_row:
            a, b = _norm(got_row[key]), _norm(expected_row[key])
            if isinstance(a, float) and isinstance(b, float):
                if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
                    return False
            elif a != b:
                return False
    return True


def test_server_is_reachable(jammi_connector):
    assert jammi_connector.test_connection()


def test_get_schema_matches_duckdb_connector(jammi_connector):
    ours = jammi_connector.get_schema()
    reference = DuckDBConnector().get_schema()

    assert set(ours.tables) == set(reference.tables)
    for table in reference.tables:
        expected = [(c["name"], c["type"], c["nullable"]) for c in reference.tables[table]]
        got = [(c["name"], c["type"], True) for c in ours.tables[table]]  # Parquet columns are nullable
        assert [n for n, *_ in got] == [n for n, *_ in expected], table
        assert [t for _, t, _ in got] == [t for _, t, _ in expected], table
        assert all(len(c["sample_values"]) <= 5 for c in ours.tables[table])
    assert ours.row_counts == reference.row_counts
    assert ours.inferred_joins == reference.inferred_joins
    # Both connectors estimate NDV (HyperLogLog); compare against the exact count, not DuckDB's
    # estimate (on category_translation DuckDB estimates 62 for 71 distinct values; Jammi 71).
    import duckdb

    exact_db = duckdb.connect(str(Path(__file__).resolve().parents[1] / "data" / "ecommerce.duckdb"), read_only=True)
    for table, counts in reference.distinct_counts.items():
        for column in counts:
            exact = exact_db.execute(f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"').fetchone()[0]
            approx = ours.distinct_counts[table][column]
            assert approx >= 0, (table, column)
            assert abs(approx - exact) <= max(2, 0.05 * exact), (table, column, approx, exact)


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.question)
def test_golden_question_end_to_end_matches_duckdb(case, jammi_connector):
    reference = _service(DuckDBConnector()).run_question(case.question)
    ours = _service(jammi_connector, table_ref=jammi_connector.table_ref).run_question(case.question)

    assert ours.intent == reference.intent
    assert ours.validation_status == reference.validation_status
    assert ours.row_count == reference.row_count
    assert _rows_match(ours.rows, reference.rows), (ours.rows[:3], reference.rows[:3])
    # Same SQL apart from the physical table references.
    stripped = ours.sql
    for table, physical in jammi_connector._tables.items():
        stripped = stripped.replace(f"{physical} AS {table}", table)
    for table, physical in jammi_connector._tables.items():
        stripped = stripped.replace(f"{physical} AS ", f"{table} ")
    assert stripped == reference.sql
    assert (ours.chart_spec or {}).get("chart_type") == (reference.chart_spec or {}).get("chart_type")


def test_concurrent_queries_on_one_shared_client(jammi_connector):
    sql = (f"SELECT customer_state, COUNT(*) AS n FROM {jammi_connector.table_ref('customers')} "
           "GROUP BY customer_state ORDER BY n DESC NULLS LAST, customer_state LIMIT 5")
    expected = jammi_connector.execute_query(sql).to_dict(orient="records")

    def run(_):
        return jammi_connector.execute_query(sql).to_dict(orient="records")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(40)))
    assert all(r == expected for r in results)


def test_other_workspace_source_is_refused_by_nlviz(jammi_client, jammi_connector):
    """Jammi would serve another workspace's source by name (issue J2); NLviz's gate must not."""
    other = f"wother{uuid.uuid4().hex[:8]}_c1_sellers"
    jammi_client.add_source(other, url=f"{DATA_DIR}/sellers.parquet", format="parquet")
    try:
        assert jammi_client.sql(f"SELECT COUNT(*) AS n FROM {other}.public.sellers").num_rows == 1  # J2
        with pytest.raises(SQLSafetyError, match="outside allowlist"):
            jammi_connector.execute_query(f"SELECT COUNT(*) AS n FROM {other}.public.sellers LIMIT 1")
    finally:
        jammi_client.remove_source(other)


def test_empty_result_keeps_columns(jammi_connector):
    df = jammi_connector.execute_query(
        f"SELECT customer_state, COUNT(*) AS n FROM {jammi_connector.table_ref('customers')} "
        "WHERE customer_state = 'nowhere' GROUP BY customer_state LIMIT 5"
    )
    assert df.empty and list(df.columns) == ["customer_state", "n"]


def test_default_demo_runtime_on_jammi_is_idempotent_and_matches_duckdb(jammi_client):
    """Startup path: the shared Olist demo registers as Jammi sources; a restart reuses them."""
    from app.dependencies import build_default_runtime

    config = IntentMapperConfig(mode="heuristic")
    # This run's own workspace: source ids are global, listing is per tenant (J14).
    workspace = f"demo-test-{uuid.uuid4().hex[:8]}"
    first = build_default_runtime(config, jammi_client=jammi_client, demo_data_url=DATA_DIR,
                                  demo_workspace_id=workspace)
    try:
        restarted = build_default_runtime(config, jammi_client=jammi_client, demo_data_url=DATA_DIR,
                                          demo_workspace_id=workspace)
        assert isinstance(restarted.connector, JammiConnector)
        assert restarted.connector.allowed_tables == first.connector.allowed_tables

        question = "Show revenue by state"
        ours = restarted.query_service.run_question(question)
        reference = _service(DuckDBConnector()).run_question(question)
        assert "AS orders" in ours.sql and ".public.orders" in ours.sql
        assert _rows_match(ours.rows, reference.rows)
    finally:
        for source_id in {p.split(".")[0] for p in first.connector.allowed_tables}:
            jammi_client.remove_source(source_id)
