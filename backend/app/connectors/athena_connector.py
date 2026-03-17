"""AWS Athena connector — queries data in S3 via the Athena SQL engine.

Users provide their own AWS credentials (access key / secret) which are stored
in the secret store. The Fargate task role is NOT used for data access, ensuring
per-user isolation.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import pandas as pd

from .base import DataConnector, SchemaContext

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUERY_TIMEOUT_S = int(os.environ.get("ATHENA_QUERY_TIMEOUT_S", "60"))


class AthenaConnector(DataConnector):
    """Athena (Presto) connector using pyathena."""

    def __init__(
        self,
        connection_params: dict[str, Any],
        denied_columns: list[str] | None = None,
    ):
        try:
            import pyathena
            from pyathena.pandas.cursor import PandasCursor
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Athena connector requires pyathena: pip install 'pyathena>=3.0.0'"
            ) from exc

        self._pyathena = pyathena
        self._PandasCursor = PandasCursor

        self._aws_access_key_id: str | None = connection_params.get("aws_access_key_id")
        self._aws_secret_access_key: str | None = connection_params.get("aws_secret_access_key")
        self._region_name: str = connection_params.get("region_name", "us-east-1")
        self._s3_staging_dir: str = connection_params["s3_staging_dir"]
        self._work_group: str = connection_params.get("work_group", "primary")
        self._catalog_name: str = connection_params.get("catalog_name", "AwsDataCatalog")
        self._database_name: str = connection_params["database_name"]
        self._denied_columns: list[str] = denied_columns or []

        self._cached_allowed_tables: set[str] | None = None
        self._cached_table_columns: dict[str, set[str]] | None = None
        self._conn = None

    def get_connector_type(self) -> str:
        return "athena"

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
            return True
        except Exception:
            logger.exception("Athena test_connection failed")
            return False

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        from app.security import validate_sql_safety

        normalized = sql.strip().rstrip(";")
        if not re.search(r"\blimit\s+\d+\b", normalized, flags=re.IGNORECASE):
            normalized = f"{normalized} LIMIT {limit}"

        validate_sql_safety(
            normalized,
            allowed_tables=self._get_allowed_tables(),
            table_columns=self._get_all_table_columns(),
            max_limit=limit,
            denied_columns=frozenset(self._denied_columns) if self._denied_columns else None,
            dialect="presto",
        )

        conn = self._connect_pandas()
        try:
            with conn.cursor() as cur:
                cur.execute(normalized)
                df = cur.fetchall()
                if isinstance(df, pd.DataFrame):
                    return df
                # Fallback: build DataFrame from tuples
                columns = [desc[0] for desc in cur.description] if cur.description else []
                return pd.DataFrame(df, columns=columns)
        finally:
            conn.close()

    def get_schema(self) -> SchemaContext:
        conn = self._connect()
        try:
            tables = self._list_tables(conn)
            table_metadata: dict[str, list[dict]] = {}
            row_counts: dict[str, int] = {}
            distinct_counts: dict[str, dict[str, int]] = {}

            for table_name in tables:
                table_metadata[table_name] = self._get_columns(conn, table_name)
                row_counts[table_name] = self._get_row_count(conn, table_name)
                distinct_counts[table_name] = {}  # skip for Athena (expensive)

            # Athena has no FK metadata — rely purely on heuristic inference
            heuristic_joins, heuristic_provenance = self._infer_heuristic_joins(
                table_metadata, set(tables)
            )

            return SchemaContext(
                tables=table_metadata,
                row_counts=row_counts,
                join_paths=[],
                distinct_counts=distinct_counts,
                inferred_joins=heuristic_joins,
                join_provenance=heuristic_provenance,
            )
        finally:
            conn.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ── Private helpers ──────────────────────────────────────────────

    def _connect(self):
        kwargs = self._base_connect_kwargs()
        return self._pyathena.connect(**kwargs)

    def _connect_pandas(self):
        kwargs = self._base_connect_kwargs()
        kwargs["cursor_class"] = self._PandasCursor
        return self._pyathena.connect(**kwargs)

    def _base_connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "s3_staging_dir": self._s3_staging_dir,
            "region_name": self._region_name,
            "work_group": self._work_group,
            "catalog_name": self._catalog_name,
            "schema_name": self._database_name,
        }
        if self._aws_access_key_id and self._aws_secret_access_key:
            kwargs["aws_access_key_id"] = self._aws_access_key_id
            kwargs["aws_secret_access_key"] = self._aws_secret_access_key
        return kwargs

    def _quote(self, identifier: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError(f"Unsafe identifier: {identifier}")
        return f'"{identifier}"'

    def _get_allowed_tables(self) -> set[str]:
        if self._cached_allowed_tables is None:
            conn = self._connect()
            try:
                self._cached_allowed_tables = set(self._list_tables(conn))
            finally:
                conn.close()
        return self._cached_allowed_tables

    def _get_all_table_columns(self) -> dict[str, set[str]]:
        if self._cached_table_columns is None:
            conn = self._connect()
            try:
                result: dict[str, set[str]] = {}
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT table_name, column_name "
                        f"FROM information_schema.columns "
                        f"WHERE table_schema = '{self._database_name}'"
                    )
                    for row in cur.fetchall():
                        result.setdefault(row[0], set()).add(row[1])
                self._cached_table_columns = result
            finally:
                conn.close()
        return self._cached_table_columns

    def _list_tables(self, conn) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{self._database_name}' "
                f"ORDER BY table_name"
            )
            return [row[0] for row in cur.fetchall()]

    def _get_columns(self, conn, table_name: str) -> list[dict]:
        columns: list[dict] = []
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT column_name, data_type, is_nullable "
                f"FROM information_schema.columns "
                f"WHERE table_schema = '{self._database_name}' "
                f"AND table_name = '{table_name}' "
                f"ORDER BY ordinal_position"
            )
            for col_name, data_type, is_nullable in cur.fetchall():
                columns.append({
                    "name": col_name,
                    "type": data_type,
                    "nullable": is_nullable == "YES",
                    "sample_values": [],  # skip sampling for Athena (expensive)
                })
        return columns

    def _get_row_count(self, conn, table_name: str) -> int:
        """Approximate row count via SHOW TABLE PROPERTIES or COUNT(*)."""
        safe_table = self._quote(table_name)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {safe_table}")
                result = cur.fetchone()
                return result[0] if result else 0
        except Exception:
            logger.warning("Failed to get row count for %s", table_name)
            return 0

    @staticmethod
    def _infer_heuristic_joins(
        table_metadata: dict[str, list[dict]],
        table_set: set[str],
    ) -> tuple[list[dict], dict[str, str]]:
        """Heuristic FK inference via *_id column naming conventions."""
        inferred: list[dict] = []
        provenance: dict[str, str] = {}
        seen_pairs: set[tuple[str, str]] = set()

        for table_name, columns in table_metadata.items():
            for col in columns:
                col_name: str = col["name"]
                if not col_name.endswith("_id"):
                    continue
                stem = col_name[: -len("_id")]
                for candidate in [stem, stem + "s", stem + "es"]:
                    if candidate in table_set and candidate != table_name:
                        pair = (min(table_name, candidate), max(table_name, candidate))
                        if pair in seen_pairs:
                            break
                        seen_pairs.add(pair)
                        target_cols = {c["name"] for c in table_metadata.get(candidate, [])}
                        if col_name in target_cols:
                            inferred.append({
                                "from": table_name,
                                "to": candidate,
                                "on": f"{table_name}.{col_name} = {candidate}.{col_name}",
                                "type": "LEFT",
                            })
                            key = f"{table_name}.{col_name}={candidate}.{col_name}"
                            provenance[key] = "heuristic"
                        break

        return inferred, provenance
