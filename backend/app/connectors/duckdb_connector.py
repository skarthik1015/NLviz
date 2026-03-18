from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
import duckdb
import pandas as pd

from .base import DataConnector, SchemaContext
from app.security import validate_sql_safety


def _sanitize_name(raw: str) -> str:
    """Produce a safe SQL identifier from a raw string (mirrors upload logic)."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    safe = re.sub(r"_+", "_", safe).strip("_") or "uploaded_table"
    if safe[0].isdigit():
        safe = f"t_{safe}"
    return safe[:63]


class DuckDBConnector(DataConnector):
    def __init__(
        self,
        db_path: str | Path | None = None,
        denied_columns: list[str] | None = None,
        table_name: str | None = None,
        aws_region: str | None = None,
    ):
        self._aws_region = aws_region or os.getenv("AWS_REGION", "us-east-1")

        db_path_str = str(db_path) if db_path else None
        if db_path_str and db_path_str.startswith("s3://"):
            self.db_path: str | Path = db_path_str
            parsed_path = PurePosixPath(urlparse(db_path_str).path)
            self._s3_suffix = parsed_path.suffix.lower()
            self._table_name: str = table_name or _sanitize_name(parsed_path.stem)
        else:
            default_db = Path(__file__).resolve().parents[2] / "data" / "ecommerce.duckdb"
            self.db_path = Path(db_path_str) if db_path_str else default_db
            self._s3_suffix = None
            self._table_name = table_name or ""

        self._identifier_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        self._denied_columns: list[str] = denied_columns or []
        self._cached_allowed_tables: set[str] | None = None
        self._cached_table_columns: dict[str, set[str]] | None = None

    @property
    def _is_s3(self) -> bool:
        return isinstance(self.db_path, str) and self.db_path.startswith("s3://")

    def get_connector_type(self) -> str:
        return "duckdb"

    def test_connection(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        normalized = sql.strip().rstrip(";")
        if not re.search(r"\blimit\s+\d+\b", normalized, flags=re.IGNORECASE):
            normalized = f"{normalized} LIMIT {limit}"
        validate_sql_safety(
            normalized,
            allowed_tables=self._get_allowed_tables(),
            table_columns=self._get_all_table_columns(),
            max_limit=limit,
            denied_columns=frozenset(self._denied_columns) if self._denied_columns else None,
        )
        with self._connect() as conn:
            return conn.execute(normalized).df()

    def _get_allowed_tables(self) -> set[str]:
        if self._cached_allowed_tables is None:
            self._cached_allowed_tables = set(self._list_tables())
        return self._cached_allowed_tables

    def _get_all_table_columns(self) -> dict[str, set[str]]:
        if self._cached_table_columns is None:
            self._cached_table_columns = self._list_table_columns()
        return self._cached_table_columns

    def get_schema(self) -> SchemaContext:
        tables = self._list_tables()
        table_metadata: dict[str, list[dict]] = {}
        row_counts: dict[str, int] = {}
        distinct_counts: dict[str, dict[str, int]] = {}

        with self._connect() as conn:
            for table_name in tables:
                safe_table = self._quote_identifier(table_name)
                table_metadata[table_name] = self._get_table_columns(conn, table_name)
                result = conn.execute(
                    f"SELECT COUNT(*) FROM {safe_table}"
                ).fetchone()
                row_counts[table_name] = result[0] if result else 0

                # Approximate NDV per column (fast, avoids full scans)
                distinct_counts[table_name] = {}
                for col_info in table_metadata[table_name]:
                    col_name = col_info["name"]
                    safe_col = self._quote_identifier(col_name)
                    try:
                        ndv_row = conn.execute(
                            f"SELECT approx_count_distinct({safe_col}) FROM {safe_table}"
                        ).fetchone()
                        distinct_counts[table_name][col_name] = ndv_row[0] if ndv_row else 0
                    except Exception:
                        distinct_counts[table_name][col_name] = -1

        table_set = set(tables)
        inferred_joins, join_provenance = self._infer_joins(table_metadata, table_set)

        return SchemaContext(
            tables=table_metadata,
            row_counts=row_counts,
            join_paths=[],
            distinct_counts=distinct_counts,
            inferred_joins=inferred_joins,
            join_provenance=join_provenance,
        )

    @staticmethod
    def _infer_joins(
        table_metadata: dict[str, list[dict]],
        table_set: set[str],
    ) -> tuple[list[dict], dict[str, str]]:
        """Heuristic FK inference: columns named *_id matching another table."""
        inferred: list[dict] = []
        provenance: dict[str, str] = {}
        seen: set[tuple[str, str]] = set()

        for table_name, columns in table_metadata.items():
            for col in columns:
                col_name: str = col["name"]
                if not col_name.endswith("_id"):
                    continue
                # e.g. customer_id → customers
                stem = col_name[: -len("_id")]
                candidates = [stem, stem + "s", stem + "es"]
                for candidate in candidates:
                    if candidate in table_set and candidate != table_name:
                        sorted_pair = sorted([table_name, candidate])
                        pair = (sorted_pair[0], sorted_pair[1])
                        if pair in seen:
                            break
                        seen.add(pair)
                        join_key = f"{table_name}.{col_name}={candidate}.{col_name}"
                        # Check if the target table actually has this column
                        target_cols = {c["name"] for c in table_metadata.get(candidate, [])}
                        if col_name in target_cols:
                            inferred.append({
                                "from": table_name,
                                "to": candidate,
                                "on": f"{table_name}.{col_name} = {candidate}.{col_name}",
                                "type": "LEFT",
                            })
                            provenance[join_key] = "heuristic"
                        break

        return inferred, provenance

    def close(self) -> None:
        # Connector uses short-lived connections; nothing persistent to close.
        return None

    def _connect(self):
        if self._is_s3:
            conn = duckdb.connect(":memory:")
            conn.execute("INSTALL httpfs; LOAD httpfs;")
            conn.execute(f"SET s3_region='{self._aws_region}';")
            self._configure_s3_credentials(conn)
            quoted = self._quote_identifier(self._table_name)
            if self._s3_suffix == ".csv":
                conn.execute(
                    f"CREATE VIEW {quoted} AS SELECT * FROM read_csv_auto('{self.db_path}')"
                )
            else:
                conn.execute(
                    f"CREATE VIEW {quoted} AS SELECT * FROM read_parquet('{self.db_path}')"
                )
            return conn
        return duckdb.connect(str(self.db_path), read_only=True)

    def _configure_s3_credentials(self, conn) -> None:
        import boto3

        session = boto3.Session(region_name=self._aws_region)
        credentials = session.get_credentials()
        if credentials is None:
            raise RuntimeError("AWS credentials not available for DuckDB S3 access")

        frozen = credentials.get_frozen_credentials()
        conn.execute(f"SET s3_access_key_id='{self._escape_sql_string(frozen.access_key)}';")
        conn.execute(f"SET s3_secret_access_key='{self._escape_sql_string(frozen.secret_key)}';")
        if frozen.token:
            conn.execute(f"SET s3_session_token='{self._escape_sql_string(frozen.token)}';")

    @staticmethod
    def _escape_sql_string(value: str) -> str:
        return value.replace("'", "''")

    def _list_tables(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                ORDER BY table_name
                """
            ).fetchall()
        return [row[0] for row in rows]

    def _quote_identifier(self, identifier: str) -> str:
        if not self._identifier_pattern.fullmatch(identifier):
            raise ValueError(f"Unsafe identifier: {identifier}")
        return f'"{identifier}"'

    def _list_table_columns(self) -> dict[str, set[str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'main'
                """
            ).fetchall()
        columns_by_table: dict[str, set[str]] = {}
        for table_name, column_name in rows:
            columns_by_table.setdefault(table_name, set()).add(column_name)
        return columns_by_table

    def _get_table_columns(self, conn, table_name: str) -> list[dict]:
        safe_table = self._quote_identifier(table_name)
        rows = conn.execute(f"PRAGMA table_info({safe_table})").fetchall()
        columns: list[dict] = []
        for _, column_name, column_type, not_null, _, _ in rows:
            safe_column = self._quote_identifier(column_name)
            sample_values = conn.execute(
                f"""
                SELECT DISTINCT {safe_column}
                FROM {safe_table}
                WHERE {safe_column} IS NOT NULL
                LIMIT 5
                """
            ).fetchall()
            columns.append(
                {
                    "name": column_name,
                    "type": column_type,
                    "nullable": not bool(not_null),
                    "sample_values": [sample[0] for sample in sample_values],
                }
            )
        return columns
