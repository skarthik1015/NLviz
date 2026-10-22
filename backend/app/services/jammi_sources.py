"""Provision and remove the Jammi sources behind one NLviz connection.

A connection's data is registered as one or more Jammi sources whose ids encode the workspace and
the connection (`w<ws>_c<conn>_<name>`). Jammi source ids are a global namespace that any session
can query by name (issue J2), so these names are NOT the isolation control. Isolation is the
connection's table mapping enforced by the SQL safety allowlist (decision AD-5). The names make
ownership legible in the Jammi catalog and keep workspaces from colliding (J14).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)

_NAME_PART = re.compile(r"[^a-z0-9_]+")
# Physical references must satisfy JammiConnector's lowercase identifier rule.
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_MAX_SOURCE_ID = 63


class JammiSourceClient(Protocol):
    def add_source(self, source_id: str, *, url: str, format: str | None = None) -> None: ...
    def remove_source(self, source_id: str) -> None: ...
    def describe(self, relation_or_query: str) -> list[dict[str, str]]: ...
    def source_ids(self) -> set[str]: ...


class ProvisioningError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSpec:
    """One Jammi source: `name` becomes part of its id; `tables` are the tables it must expose."""

    name: str
    url: str
    tables: list[str]
    format: str | None = None


@dataclass
class ProvisionedConnection:
    tables: dict[str, str] = field(default_factory=dict)  # logical -> "<source>.public.<table>"
    skipped_tables: list[str] = field(default_factory=list)

    @property
    def source_ids(self) -> list[str]:
        return sorted({physical.split(".", 1)[0] for physical in self.tables.values()})


def _slug(value: str, length: int) -> str:
    slug = _NAME_PART.sub("_", value.lower()).strip("_")
    return slug[:length] or "x"


def workspace_tag(workspace_id: str) -> str:
    """Stable 8-hex tag for a workspace id (which may be an email-like or long Cognito sub)."""
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:8]


def source_id_for(workspace_id: str, connection_id: str, name: str) -> str:
    conn = _slug(connection_id.replace("-", ""), 12)
    source_id = f"w{workspace_tag(workspace_id)}_c{conn}_{_slug(name, 40)}"
    return source_id[:_MAX_SOURCE_ID]


# Source names used for whole-database (federated) sources; files use their table name.
DATABASE_SOURCE_NAMES = frozenset({"pg", "mysql"})


def database_source_ids(workspace_id: str | None, connection_id: str, tables: dict[str, str]) -> set[str]:
    """The connection's live database sources (Postgres, or MySQL in federation mode)."""
    if not workspace_id:
        return set()
    candidates = {source_id_for(workspace_id, connection_id, name) for name in DATABASE_SOURCE_NAMES}
    return candidates & {physical.split(".", 1)[0] for physical in tables.values()}


def postgres_url(params: dict) -> str:
    """postgresql:// URL for a Jammi Postgres source from NLviz's Postgres form params
    (the same keys PostgresConnector reads: host, port, dbname, user, password, optional sslmode)."""
    user = quote(str(params["user"]), safe="")
    password = quote(str(params.get("password", "")), safe="")
    host = params.get("host", "localhost")
    port = int(params.get("port", 5432))
    database = quote(str(params["dbname"]), safe="")
    auth = f"{user}:{password}@" if password else f"{user}@"
    url = f"postgresql://{auth}{host}:{port}/{database}"
    sslmode = params.get("sslmode")
    return f"{url}?sslmode={quote(str(sslmode), safe='')}" if sslmode else url


def mysql_url(params: dict) -> str:
    """mysql:// URL for a Jammi MySQL source from NLviz's MySQL form params (same keys as Postgres:
    host, port, dbname, user, password). TLS is not configurable here: Jammi always verifies the
    server certificate against jammi-server's trust store (issue J26)."""
    user = quote(str(params["user"]), safe="")
    password = quote(str(params.get("password", "")), safe="")
    host = params.get("host", "localhost")
    port = int(params.get("port", 3306))
    database = quote(str(params["dbname"]), safe="")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"mysql://{auth}{host}:{port}/{database}"


class JammiSourceProvisioner:
    def __init__(self, client: JammiSourceClient):
        self._client = client

    def provision(
        self,
        *,
        workspace_id: str,
        connection_id: str,
        specs: list[SourceSpec],
        reuse_existing: bool = False,
    ) -> ProvisionedConnection:
        """Register every spec's source and verify every table resolves; all or nothing.

        reuse_existing: keep a source that is already registered under the same id (used for the
        bundled demo dataset at startup). Otherwise an existing id is an error.
        """
        if not specs:
            raise ProvisioningError("No sources to provision")
        existing = self._client.source_ids() if reuse_existing else set()
        added: list[str] = []
        result = ProvisionedConnection()
        try:
            for spec in specs:
                source_id = source_id_for(workspace_id, connection_id, spec.name)
                if source_id not in existing:
                    self._client.add_source(source_id, url=spec.url, format=spec.format)
                    added.append(source_id)
                for table in spec.tables:
                    if not _TABLE_NAME.fullmatch(table):
                        # Jammi resolves unquoted lowercase identifiers; mixed-case or symbol table
                        # names would need quoting throughout the compiler, so they are skipped.
                        result.skipped_tables.append(table)
                        continue
                    if table in result.tables:
                        raise ProvisioningError(f"Table {table!r} is provided by more than one source")
                    physical = f"{source_id}.public.{table}"
                    self._client.describe(physical)  # raises if the table does not resolve
                    result.tables[table] = physical
            if not result.tables:
                raise ProvisioningError("None of the connection's tables can be queried")
            return result
        except Exception:
            self._rollback(added)
            raise

    def deprovision(self, tables: dict[str, str]) -> list[str]:
        """Remove every source behind a connection's mapping; returns the ids that failed."""
        failed: list[str] = []
        for source_id in sorted({physical.split(".", 1)[0] for physical in tables.values()}):
            try:
                self._client.remove_source(source_id)
            except Exception:
                # The source row (with any credentials in its URL, issue J6) stays in the catalog.
                logger.warning("Failed to remove Jammi source %s", source_id, exc_info=True)
                failed.append(source_id)
        return failed

    def _rollback(self, added: list[str]) -> None:
        for source_id in reversed(added):
            try:
                self._client.remove_source(source_id)
            except Exception:
                logger.warning("Rollback could not remove Jammi source %s", source_id, exc_info=True)
