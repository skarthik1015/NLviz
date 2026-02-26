from __future__ import annotations

from pathlib import Path
import re

import duckdb
import pandas as pd

from .base import DataConnector, SchemaContext
from app.security import validate_sql_safety


class DuckDBConnector(DataConnector):
    def __init__(self, db_path: str | Path | None = None):
        default_db = Path(__file__).resolve().parents[2] / "data" / "ecommerce.duckdb"
        self.db_path = Path(db_path) if db_path else default_db
        self.conn = duckdb.connect(str(self.db_path))

    def get_connector_type(self) -> str:
        return "duckdb"

    def test_connection(self) -> bool:
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        normalized = sql.strip().rstrip(";")
        if not re.search(r"\blimit\s+\d+\b", normalized, flags=re.IGNORECASE):
            normalized = f"{normalized} LIMIT {limit}"
        validate_sql_safety(
            normalized,
            allowed_tables=set(self._list_tables()),
            max_limit=limit,
        )
        return self.conn.execute(normalized).df()

    def get_schema(self) -> SchemaContext:
        tables = self._list_tables()
        table_metadata: dict[str, list[dict]] = {}
        row_counts: dict[str, int] = {}

        for table_name in tables:
            table_metadata[table_name] = self._get_table_columns(table_name)
            row_counts[table_name] = self.conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]

        join_paths = self._infer_join_paths(table_metadata)
        return SchemaContext(
            tables=table_metadata,
            row_counts=row_counts,
            join_paths=join_paths,
        )

    def _list_tables(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_name
            """
        ).fetchall()
        return [row[0] for row in rows]

    def _get_table_columns(self, table_name: str) -> list[dict]:
        rows = self.conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        columns: list[dict] = []
        for _, column_name, column_type, not_null, _, _ in rows:
            sample_values = self.conn.execute(
                f"""
                SELECT DISTINCT {column_name}
                FROM {table_name}
                WHERE {column_name} IS NOT NULL
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

    def _infer_join_paths(self, tables: dict[str, list[dict]]) -> list[dict]:
        id_columns_by_table = {
            table_name: {column["name"] for column in columns if column["name"].endswith("_id")}
            for table_name, columns in tables.items()
        }

        join_paths: list[dict] = []
        for from_table, from_columns in id_columns_by_table.items():
            for to_table, to_columns in id_columns_by_table.items():
                if from_table == to_table:
                    continue
                common_ids = sorted(from_columns.intersection(to_columns))
                for common_id in common_ids:
                    join_paths.append(
                        {
                            "from_table": from_table,
                            "to_table": to_table,
                            "from_col": common_id,
                            "to_col": common_id,
                        }
                    )
        return join_paths
