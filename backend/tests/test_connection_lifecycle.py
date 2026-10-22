"""Connection lifecycle under change (review B12, B13): publish, eviction and background builds."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.connection import ConnectionProfile, SemanticSchemaVersion
from app.services.connection_service import ConnectionService
from app.services.connection_store import JsonlConnectionStore
from app.services.intent_mapper import IntentMapperConfig


def _store(tmp_path) -> JsonlConnectionStore:
    store = JsonlConnectionStore(tmp_path / "c.jsonl", tmp_path / "v.jsonl")
    store.create_connection(ConnectionProfile(connection_id="c1", display_name="c", connector_type="upload",
                                              created_at=datetime.now(timezone.utc), owner_id="u", engine="jammi"))
    for vid in ("v1", "v2", "v3"):
        store.create_version(SemanticSchemaVersion(version_id=vid, connection_id="c1", status="validated",
                                                   created_at=datetime.now(timezone.utc), schema_path=f"{vid}.yaml"))
    return store


class _Service(ConnectionService):
    """Runtimes are labelled with their schema path; "broken.yaml" cannot load."""

    def _build_connector(self, profile):
        return SimpleNamespace()

    def _build_runtime(self, connector, schema_path, profile=None):
        if schema_path == "broken.yaml":
            raise FileNotFoundError(schema_path)
        return SimpleNamespace(connector=connector, schema_path=schema_path)


def test_a_schema_that_cannot_load_is_never_recorded_as_published(tmp_path):
    store = _store(tmp_path)
    service = _Service(store, None, IntentMapperConfig(mode="heuristic"))
    service.activate_schema("c1", "v1")
    store.update_version(store.get_version("v2").model_copy(update={"schema_path": "broken.yaml"}))
    with pytest.raises(FileNotFoundError):
        service.activate_schema("c1", "v2")
    assert store.get_published_version("c1").version_id == "v1"  # unchanged
    assert service.get_runtime("c1").schema_path == "v1.yaml"


def test_concurrent_publishes_leave_exactly_one_published(tmp_path):
    store = _store(tmp_path)
    service = _Service(store, None, IntentMapperConfig(mode="heuristic"))
    errors = []

    def publish(vid):
        try:
            service.activate_schema("c1", vid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(vid,)) for vid in ("v1", "v2", "v3")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    published = [v for v in store.list_versions("c1") if v.status == "published"]
    assert len(published) == 1 and not errors
    assert service.get_runtime("c1").schema_path == published[0].schema_path


def test_a_runtime_loaded_across_an_eviction_is_not_cached(tmp_path):
    store = _store(tmp_path)
    service = _Service(store, None, IntentMapperConfig(mode="heuristic"))
    service.activate_schema("c1", "v1")
    service.evict_runtime("c1")
    original = service._load_runtime

    def slow_load(connection_id):
        runtime = original(connection_id)
        service.evict_runtime(connection_id)  # deleted while this load was running
        return runtime

    service._load_runtime = slow_load
    service.get_runtime("c1")
    assert not service.has_runtime("c1")


class _FakeBuilder:
    def __init__(self, on_build=None):
        self.on_build = on_build

    def build(self, **kwargs):
        if self.on_build:
            self.on_build()
        return "index"


def test_an_index_finished_after_delete_is_not_installed_and_its_sources_are_removed(monkeypatch):
    import app.grounding.service as service_module

    removed = []
    monkeypatch.setattr(service_module, "extract_values", lambda *a, **k: {"dim": ["a"]})
    monkeypatch.setattr(service_module, "remove_grounding_sources", lambda *a: removed.append("values"))
    monkeypatch.setattr(service_module, "remove_example_sources", lambda *a: removed.append("examples"))
    grounding = service_module.GroundingService(client=None, store=None, examples_enabled=False)
    grounding._builder = _FakeBuilder(on_build=lambda: grounding.remove(connection_id="c1", workspace_id="w"))
    connector = SimpleNamespace(table_ref_resolver=lambda: None)

    grounding.ensure(connection_id="c1", workspace_id="w", connector=connector, registry=None).result()

    assert grounding.index_for("c1") is None
    assert removed.count("values") == 2  # by remove(), then again for what the finished build registered
    # A new build after the delete is not blocked by the stale one.
    grounding._builder = _FakeBuilder()
    grounding.ensure(connection_id="c1", workspace_id="w", connector=connector, registry=None).result()
    assert grounding.index_for("c1") == "index"


def test_snapshot_provenance_survives_a_reload(tmp_path):
    store = JsonlConnectionStore(tmp_path / "c.jsonl", tmp_path / "v.jsonl")
    copied = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    store.create_connection(ConnectionProfile(connection_id="m1", display_name="m", connector_type="mysql",
                                              created_at=copied, owner_id="u", engine="jammi", mode="snapshot",
                                              copied_at=copied, notes=["Copied when you connected."]))
    again = JsonlConnectionStore(tmp_path / "c.jsonl", tmp_path / "v.jsonl").get_connection("m1")
    assert (again.mode, again.copied_at, again.notes) == ("snapshot", copied, ["Copied when you connected."])


def test_grounding_readiness_is_reported(monkeypatch):
    import app.grounding.service as service_module

    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(service_module, "extract_values", lambda *a, **k: {"dim": ["a"]})
    grounding = service_module.GroundingService(client=None, store=None, examples_enabled=False)
    grounding._builder = _FakeBuilder(on_build=lambda: (started.set(), release.wait(5)))
    assert grounding.status("c1") == "none"
    future = grounding.ensure(connection_id="c1", workspace_id="w",
                              connector=SimpleNamespace(table_ref_resolver=lambda: None), registry=None)
    started.wait(5)
    assert grounding.status("c1") == "building"
    release.set()
    future.result()
    assert grounding.status("c1") == "ready"


def test_a_build_for_the_old_schema_never_installs_into_the_new_one(monkeypatch):
    """Second review: publishing while the old schema's index was building skipped the new build,
    and the old build then installed the old schema's index."""
    import app.grounding.service as service_module

    removed = []
    monkeypatch.setattr(service_module, "extract_values", lambda *a, **k: {"dim": ["a"]})
    monkeypatch.setattr(service_module, "remove_grounding_sources", lambda *a: removed.append("values"))
    monkeypatch.setattr(service_module, "remove_example_sources", lambda *a: removed.append("examples"))
    grounding = service_module.GroundingService(client=None, store=None, examples_enabled=False)
    started, release = threading.Event(), threading.Event()
    connector = SimpleNamespace(table_ref_resolver=lambda: None)

    class Builder:
        def build(self, *, registry, **kwargs):
            if registry == "old":
                started.set()
                release.wait(5)
            return f"index for {registry}"

    grounding._builder = Builder()
    old = grounding.ensure(connection_id="c1", workspace_id="w", connector=connector, registry="old",
                           schema_key="v1.yaml")
    started.wait(5)
    new = grounding.ensure(connection_id="c1", workspace_id="w", connector=connector, registry="new",
                           schema_key="v2.yaml")
    assert new is not None  # not skipped because the old schema's build is running
    release.set()
    assert old.result() is None  # the old schema's build installed nothing
    new.result()
    assert grounding.index_for("c1") == "index for new"
    assert removed == []  # a new schema is not a delete: no sources removed behind the new build
