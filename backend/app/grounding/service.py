"""Builds and holds each connection's value index, in the background.

Building an index embeds every value on jammi-server (about a minute for a few thousand values on
CPU), so it never blocks a request: questions are answered without grounding until the index is
ready, then grounding switches on. An unchanged corpus reuses its sources across restarts.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.connectors.base import DataConnector
from app.grounding.examples import ExampleIndex, ExampleIndexBuilder, remove_example_sources
from app.grounding.values import ValueIndex, ValueIndexBuilder, extract_values, remove_grounding_sources
from app.semantic import SemanticRegistry, build_sql_from_intent

logger = logging.getLogger(__name__)


class GroundingService:
    def __init__(self, client, store, *, max_workers: int = 1, examples_enabled: bool = True):
        self._client = client
        self._examples_enabled = examples_enabled
        self._store = store
        self._builder = ValueIndexBuilder(client, store)
        self._example_builder = ExampleIndexBuilder(client, store)
        self._indexes: dict[str, ValueIndex] = {}
        self._examples: dict[str, ExampleIndex] = {}
        # connection -> the generation being built. remove() bumps the generation, so a build that
        # finishes after the connection was deleted installs nothing and cleans up what it
        # registered (review B13: it re-registered sources and reinstalled the index).
        self._building: dict[str, int] = {}
        self._generations: dict[str, int] = {}
        # The schema each connection's indexes are for. A new schema (a publish) starts a new
        # generation, so a build still running for the old schema installs nothing (second review:
        # ensure() for the new schema was skipped while the old build ran, which then installed the
        # old schema's indexes into the new runtime).
        self._schema_keys: dict[str, str] = {}
        self._removed: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="grounding")

    def status(self, connection_id: str) -> str:
        """"ready" (value index built), "building", or "none" (no index: nothing to index, or failed)."""
        with self._lock:
            if connection_id in self._indexes:
                return "ready"
            return "building" if connection_id in self._building else "none"

    def index_for(self, connection_id: str) -> ValueIndex | None:
        return self._indexes.get(connection_id)

    def lookup(self, connection_id: str):
        """A zero-argument callable for IntentMapperRouter(value_index=...)."""
        return lambda: self._indexes.get(connection_id)

    def example_lookup(self, connection_id: str):
        """A zero-argument callable for IntentMapperRouter(example_index=...)."""
        return lambda: self._examples.get(connection_id)

    def ensure(
        self, *, connection_id: str, workspace_id: str, connector: DataConnector, registry: SemanticRegistry,
        schema_key: str | None = None,
    ):
        """Start building the connection's index unless it is already building for this schema.
        `schema_key` names the published schema (its path); a different one invalidates what was
        built or is being built for the previous schema."""
        with self._lock:
            previous = self._schema_keys.get(connection_id)
            if schema_key is not None and previous is not None and previous != schema_key:
                self._generations[connection_id] = self._generations.get(connection_id, 0) + 1
                self._indexes.pop(connection_id, None)
                self._examples.pop(connection_id, None)
            if schema_key is not None:
                self._schema_keys[connection_id] = schema_key
            generation = self._generations.get(connection_id, 0)
            if self._building.get(connection_id) == generation:
                return None
            self._building[connection_id] = generation
        return self._pool.submit(self._build, connection_id, workspace_id, connector, registry, generation)

    def _current(self, connection_id: str, generation: int) -> bool:
        with self._lock:
            return self._generations.get(connection_id, 0) == generation

    def _install(self, target: dict, connection_id: str, generation: int, value) -> bool:
        """Install a built index unless the connection was removed meanwhile."""
        with self._lock:
            if self._generations.get(connection_id, 0) != generation:
                return False
            target[connection_id] = value
            return True

    def _build(self, connection_id: str, workspace_id: str, connector: DataConnector, registry: SemanticRegistry,
               generation: int = 0):
        try:
            return self._build_indexes(connection_id, workspace_id, connector, registry, generation)
        finally:
            with self._lock:
                if self._building.get(connection_id) == generation:
                    del self._building[connection_id]
            with self._lock:
                removed = connection_id in self._removed
            if removed and not self._current(connection_id, generation):
                # Deleted while building: what this build registered on Jammi must go too. (Not on a
                # new schema: the next build reuses or replaces those sources itself.)
                self._remove_sources(connection_id, workspace_id)

    def _build_indexes(self, connection_id: str, workspace_id: str, connector: DataConnector,
                       registry: SemanticRegistry, generation: int = 0) -> ValueIndex | None:
        try:
            resolver = connector.table_ref_resolver()
            values = extract_values(
                registry,
                run_query=lambda sql: connector.execute_query(sql).to_dict("records"),
                build_sql=lambda intent, reg: build_sql_from_intent(intent, reg, table_ref=resolver),
            )
        except Exception:
            logger.warning("Value extraction for %s failed", connection_id, exc_info=True)
            values = {}
        # Examples before the value index: a few dozen rows, built in seconds.
        if self._examples_enabled:
            try:
                examples = self._example_builder.build(workspace_id=workspace_id, connection_id=connection_id,
                                                       registry=registry)
                if examples is not None:
                    self._install(self._examples, connection_id, generation, examples)
            except Exception:
                logger.warning("Building the examples index for %s failed; using static examples",
                               connection_id, exc_info=True)
        if not values or not self._current(connection_id, generation):
            return None
        try:
            index = self._builder.build(
                workspace_id=workspace_id, connection_id=connection_id, registry=registry, values=values
            )
        except Exception:
            logger.warning("Building the value index for %s failed; continuing without grounding",
                           connection_id, exc_info=True)
            return None
        if index is not None and not self._install(self._indexes, connection_id, generation, index):
            return None
        return index

    def remove(self, *, connection_id: str, workspace_id: str) -> None:
        with self._lock:
            self._generations[connection_id] = self._generations.get(connection_id, 0) + 1
            self._indexes.pop(connection_id, None)
            self._examples.pop(connection_id, None)
            self._schema_keys.pop(connection_id, None)
            self._removed.add(connection_id)
        self._remove_sources(connection_id, workspace_id)

    def _remove_sources(self, connection_id: str, workspace_id: str) -> None:
        remove_grounding_sources(self._client, self._store, workspace_id, connection_id)
        remove_example_sources(self._client, self._store, workspace_id, connection_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
