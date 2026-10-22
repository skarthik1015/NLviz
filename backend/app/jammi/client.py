"""NLviz's single connection to jammi-server.

Design decision AD-5 (docs/architecture/current-architecture.md): isolation is enforced by NLviz, not
delegated to the engine's tenant scoping, so NLviz:
  * binds every connection to ONE fixed service tenant and never runs unscoped (J3), so the
    process-wide Flight binding (J1) only ever holds one value;
  * enforces workspace isolation itself (source naming + the SQL safety allowlist).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

if TYPE_CHECKING:
    from app.config import AppConfig


class JammiConfigError(ValueError):
    pass


@dataclass(frozen=True)
class JammiSettings:
    url: str
    service_tenant: str

    @classmethod
    def from_config(cls, config: "AppConfig") -> "JammiSettings | None":
        """None when Jammi is not configured (legacy connectors only)."""
        if not config.jammi_url:
            return None
        if not config.jammi_service_tenant:
            raise JammiConfigError("JAMMI_SERVICE_TENANT must be set when JAMMI_URL is (a UUID v4 or v7)")
        return cls(url=config.jammi_url, service_tenant=_validated_tenant(config.jammi_service_tenant))


def _validated_tenant(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise JammiConfigError(f"JAMMI_SERVICE_TENANT is not a UUID: {value!r}") from exc
    # Jammi documents v4/v7 but also accepts v1 (issue J18); NLviz holds itself to the docs.
    if parsed.version not in (4, 7):
        raise JammiConfigError(f"JAMMI_SERVICE_TENANT must be a UUID v4 or v7, got v{parsed.version}")
    return str(parsed)


class JammiClient:
    """Thin, thread-shareable wrapper around one `jammi.RemoteDatabase`."""

    def __init__(self, settings: JammiSettings, *, connect: Callable[[str], Any] | None = None):
        if connect is None:
            import jammi

            connect = jammi.connect
        self._settings = settings
        self._db = connect(settings.url)
        self._db.set_tenant(settings.service_tenant)
        # Create the lazily-initialised Flight client now, before request threads share this
        # object: its initialisation is an unlocked check-then-set in jammi 0.54.0 (issue J20).
        self._db.sql("SELECT 1")

    @property
    def service_tenant(self) -> str:
        return self._settings.service_tenant

    def sql(self, query: str) -> pa.Table:
        return self._db.sql(query)

    def describe(self, relation_or_query: str) -> list[dict[str, str]]:
        """Columns of a table or query: [{column_name, data_type, is_nullable}].

        DESCRIBE works over Flight SQL without information_schema, which a remote client
        cannot enable (issue J22).
        """
        return self._db.sql(f"DESCRIBE {relation_or_query}").to_pylist()

    def ping(self) -> bool:
        try:
            self._db.sql("SELECT 1")
            return True
        except Exception:
            return False

    def add_source(self, source_id: str, *, url: str, format: str | None = None) -> None:
        if format is None:
            self._db.add_source(source_id, url=url)
        else:
            self._db.add_source(source_id, url=url, format=format)

    def remove_source(self, source_id: str) -> None:
        """Remove a source and its derived state.

        The server has CatalogService.RemoveSource but the 0.54.0 Python client does not wrap
        it (issue J23), so this calls the generated stub. Keep this the only private-API use.
        """
        from jammi._database import catalog_pb2

        self._db._call(self._db._catalog.RemoveSource, catalog_pb2.RemoveSourceRequest(source_id=source_id))

    def source_ids(self) -> set[str]:
        return {source["source_id"] for source in self._db.list_sources()}


    # ── Retrieval verbs (grounding) ──────────────────────────────────

    def generate_embeddings(self, *, source: str, model: str, columns: list[str], key: str) -> str:
        return self._db.generate_embeddings(source=source, model=model, columns=columns, key=key)

    def build_lexical_index(self, source: str, *, columns: list[str], key: str, analyzer: str) -> str:
        return self._db.build_lexical_index(source, columns=columns, key=key, analyzer=analyzer)

    def encode_query(self, *, model: str, query: str) -> list[float]:
        return self._db.encode_query(model=model, query=query)

    def search(self, source: str, *, query: list[float], k: int, filter: str | None = None) -> pa.Table:
        return self._db.search(source, query=query, k=k, filter=filter)

    def lexical_search(self, source: str, *, text: str, k: int, filter: str | None = None) -> pa.Table:
        return self._db.lexical_search(source, text=text, k=k, filter=filter)

    def rrf_fuse(self, ranked_lists: list[list[str]]) -> list[tuple[str, float]]:
        return self._db.rrf_fuse(ranked_lists)
