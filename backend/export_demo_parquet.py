"""Export the bundled Olist demo (data/ecommerce.duckdb) as one Parquet file per table, for Jammi.

    python export_demo_parquet.py <out_dir>

Idempotent: tables whose file already exists are skipped. In Docker Compose the backend runs this
at startup into a volume that jammi-server mounts read-only (JAMMI_DEMO_DATA_URL).
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

DUCKDB_PATH = Path(__file__).resolve().parent / "data" / "ecommerce.duckdb"


def export(out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    conn = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' ORDER BY 1").fetchall()]
        for table in tables:
            target = out_dir / f"{table}.parquet"
            if target.exists():
                continue
            partial = target.with_suffix(".parquet.tmp")
            conn.execute(f"COPY (SELECT * FROM \"{table}\") TO '{partial.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            partial.replace(target)  # jammi-server never sees a half-written file
            written.append(table)
    finally:
        conn.close()
    return written


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "data" / "parquet" / "olist"
    done = export(out)
    print(f"demo parquet in {out}: exported {done or 'nothing (already present)'}")
