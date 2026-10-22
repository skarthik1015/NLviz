"""Unit tests for Jammi source provisioning and engine wiring (no server needed)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pytest

from app.connectors.jammi_connector import JammiConnector
from app.models.connection import ConnectionProfile
from app.semantic import load_semantic_registry
from app.services.connection_service import ConnectionResolutionError, ConnectionService
from app.services.connection_store import ConnectionStore
from app.services.intent_mapper import IntentMapperConfig
from app.services.jammi_sources import (
    JammiSourceProvisioner,
    ProvisioningError,
    SourceSpec,
    postgres_url,
    source_id_for,
    workspace_tag,
)
from app.services.schema_validator import validate_schema
from app.services.secret_store import SecretStore

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"
PHYSICAL = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*")


class FakeSourceClient:
    def __init__(self, *, existing=(), missing_tables=(), fail_add_on=None):
        self.sources: dict[str, str] = {s: "pre-existing" for s in existing}
        self.missing_tables = set(missing_tables)
        self.fail_add_on = fail_add_on
        self.removed: list[str] = []

    def add_source(self, source_id, *, url, format=None):
        if self.fail_add_on and self.fail_add_on in source_id:
            raise RuntimeError("cannot reach database")
        if source_id in self.sources:
            raise RuntimeError("UNIQUE constraint failed: sources.source_id")
        self.sources[source_id] = url

    def remove_source(self, source_id):
        self.removed.append(source_id)
        self.sources.pop(source_id, None)

    def describe(self, relation):
        if relation.split(".")[-1] in self.missing_tables:
            raise RuntimeError(f"table '{relation}' not found")
        return [{"column_name": "id", "data_type": "Int64", "is_nullable": "NO"}]

    def source_ids(self):
        return set(self.sources)

    # used by JammiConnector in the engine-wiring tests
    def sql(self, query):
        return pa.table({"x": [1]})

    def ping(self):
        return True


# --- naming ---


def test_source_ids_are_valid_lowercase_identifiers_and_namespaced():
    sid = source_id_for("Cognito-Sub|ABC@example.com", "123e4567-e89b-42d3-a456-556642440000", "Sales DB!")
    assert re.fullmatch(r"w[0-9a-f]{8}_c[0-9a-f]{12}_[a-z0-9_]+", sid)
    assert PHYSICAL.fullmatch(f"{sid}.public.orders")
    assert len(sid) <= 63
    assert source_id_for("user-a", "conn", "db") != source_id_for("user-b", "conn", "db")
    assert workspace_tag("user-a") == workspace_tag("user-a")


def test_postgres_url_percent_encodes_credentials():
    # Same keys the Postgres form sends and PostgresConnector reads.
    url = postgres_url({"host": "db.internal", "port": 5433, "dbname": "sales", "user": "rep@ort",
                        "password": "p:ss/w@rd#1", "sslmode": "require"})
    assert url == "postgresql://rep%40ort:p%3Ass%2Fw%40rd%231@db.internal:5433/sales?sslmode=require"
    assert postgres_url({"host": "h", "dbname": "d", "user": "u"}) == "postgresql://u@h:5432/d"


# --- provisioning ---


def test_provision_registers_sources_and_maps_tables():
    client = FakeSourceClient()
    result = JammiSourceProvisioner(client).provision(
        workspace_id="user-a",
        connection_id="c1",
        specs=[SourceSpec(name="pg", url="postgresql://u@h/d", tables=["orders", "customers"])],
    )
    sid = source_id_for("user-a", "c1", "pg")
    assert result.tables == {"orders": f"{sid}.public.orders", "customers": f"{sid}.public.customers"}
    assert result.source_ids == [sid]
    assert all(PHYSICAL.fullmatch(p) for p in result.tables.values())


def test_provision_skips_tables_jammi_cannot_address_unquoted():
    client = FakeSourceClient()
    result = JammiSourceProvisioner(client).provision(
        workspace_id="u", connection_id="c", specs=[SourceSpec(name="pg", url="x", tables=["orders", "MixedCase", "has-dash"])]
    )
    assert list(result.tables) == ["orders"]
    assert result.skipped_tables == ["MixedCase", "has-dash"]


def test_provision_rolls_back_every_added_source_on_failure():
    client = FakeSourceClient(fail_add_on="second")
    with pytest.raises(RuntimeError, match="cannot reach database"):
        JammiSourceProvisioner(client).provision(
            workspace_id="u", connection_id="c",
            specs=[SourceSpec(name="first", url="a", tables=["t1"]), SourceSpec(name="second", url="b", tables=["t2"])],
        )
    assert client.sources == {}
    assert client.removed == [source_id_for("u", "c", "first")]


def test_provision_rolls_back_when_a_table_does_not_resolve():
    client = FakeSourceClient(missing_tables={"ghost"})
    with pytest.raises(RuntimeError, match="not found"):
        JammiSourceProvisioner(client).provision(
            workspace_id="u", connection_id="c", specs=[SourceSpec(name="pg", url="x", tables=["orders", "ghost"])]
        )
    assert client.sources == {}


def test_provision_refuses_when_no_table_is_usable_and_rolls_back():
    client = FakeSourceClient()
    with pytest.raises(ProvisioningError, match="None of the connection's tables"):
        JammiSourceProvisioner(client).provision(
            workspace_id="u", connection_id="c", specs=[SourceSpec(name="pg", url="x", tables=["UPPER"])]
        )
    assert client.sources == {}


def test_provision_refuses_duplicate_logical_tables():
    client = FakeSourceClient()
    with pytest.raises(ProvisioningError, match="more than one source"):
        JammiSourceProvisioner(client).provision(
            workspace_id="u", connection_id="c",
            specs=[SourceSpec(name="a", url="x", tables=["orders"]), SourceSpec(name="b", url="y", tables=["orders"])],
        )
    assert client.sources == {}


def test_reuse_existing_keeps_preregistered_sources_and_never_removes_them():
    sid = source_id_for("demo", "default", "orders")
    client = FakeSourceClient(existing={sid})
    result = JammiSourceProvisioner(client).provision(
        workspace_id="demo", connection_id="default",
        specs=[SourceSpec(name="orders", url="/data/olist/orders.parquet", format="parquet", tables=["orders"])],
        reuse_existing=True,
    )
    assert result.tables == {"orders": f"{sid}.public.orders"}
    assert client.sources[sid] == "pre-existing"
    assert client.removed == []


def test_existing_id_without_reuse_is_an_error():
    sid = source_id_for("u", "c", "pg")
    client = FakeSourceClient(existing={sid})
    with pytest.raises(RuntimeError, match="UNIQUE"):
        JammiSourceProvisioner(client).provision(
            workspace_id="u", connection_id="c", specs=[SourceSpec(name="pg", url="x", tables=["orders"])]
        )
    assert sid in client.sources  # never removes a source it did not add


def test_deprovision_removes_each_source_once_and_reports_failures():
    client = FakeSourceClient()

    def failing_remove(source_id):
        client.removed.append(source_id)
        if source_id == "sb":
            raise RuntimeError("down")

    client.remove_source = failing_remove  # type: ignore[method-assign]
    failed = JammiSourceProvisioner(client).deprovision(
        {"a": "sa.public.a", "b": "sb.public.b", "c": "sa.public.c"}
    )
    assert client.removed == ["sa", "sb"]
    assert failed == ["sb"]


# --- engine wiring ---


def _profile(engine: str, tables: dict[str, str] | None = None) -> ConnectionProfile:
    return ConnectionProfile(
        connection_id="123e4567-e89b-42d3-a456-556642440000",
        display_name="pg",
        connector_type="postgres",
        created_at=datetime.now(timezone.utc),
        owner_id="u",
        engine=engine,
        workspace_id="u",
        jammi_tables=tables or {},
    )


def _service(tmp_path: Path, jammi_client=None) -> ConnectionService:
    return ConnectionService(
        connection_store=ConnectionStore(
            connections_path=tmp_path / "c.jsonl", versions_path=tmp_path / "v.jsonl"
        ),
        secret_store=SecretStore(secrets_dir=tmp_path / ".secrets"),
        intent_config=IntentMapperConfig(mode="heuristic"),
        jammi_client=jammi_client,
    )


def test_jammi_profile_builds_jammi_connector_from_its_mapping(tmp_path: Path):
    tables = {"orders": "wa_c1_orders.public.orders"}
    connector = _service(tmp_path, FakeSourceClient()).build_connector(_profile("jammi", tables))
    assert isinstance(connector, JammiConnector)
    assert connector.table_ref_resolver()("orders") == "wa_c1_orders.public.orders"


def test_jammi_profile_without_configured_engine_is_not_resolvable(tmp_path: Path):
    with pytest.raises(ConnectionResolutionError, match="jammi engine not configured"):
        _service(tmp_path, None).build_connector(_profile("jammi", {"orders": "wa_c1_o.public.orders"}))


def test_jammi_profiles_round_trip_through_the_jsonl_store(tmp_path: Path):
    store = ConnectionStore(connections_path=tmp_path / "c.jsonl", versions_path=tmp_path / "v.jsonl")
    profile = _profile("jammi", {"orders": "wa_c1_orders.public.orders"})
    store.create_connection(profile)
    reloaded = ConnectionStore(connections_path=tmp_path / "c.jsonl", versions_path=tmp_path / "v.jsonl")
    assert reloaded.get_connection(profile.connection_id) == profile


def test_legacy_profiles_default_to_legacy_engine():
    legacy = ConnectionProfile.model_validate(
        {"connection_id": "x", "display_name": "d", "connector_type": "duckdb", "created_at": "2026-01-01T00:00:00Z"}
    )
    assert legacy.engine == "legacy" and legacy.jammi_tables == {} and legacy.workspace_id is None


def test_schema_validator_probes_through_the_connectors_resolver():
    """Without the resolver every probe would be unqualified and refused by the gate."""
    executed: list[str] = []

    class Client(FakeSourceClient):
        def sql(self, query):
            executed.append(query)
            return pa.table({"metric_value": [1]})

    tables = {t: f"wa_c1_{t}.public.{t}" for t in
              ["orders", "order_items", "order_payments", "order_reviews", "customers", "sellers", "products"]}
    connector = JammiConnector(Client(), tables)  # type: ignore[arg-type]
    registry = load_semantic_registry(SCHEMA_PATH)

    from app.connectors.base import SchemaContext

    ctx = SchemaContext(tables={t: [] for t in tables}, row_counts={}, join_paths=[])
    summary = validate_schema(registry.schema, connector, ctx)

    assert summary.broken_metrics == [] and summary.broken_dimensions == []
    assert summary.confidence_score == 1.0
    assert any("wa_c1_orders.public.orders AS orders" in q for q in executed)
