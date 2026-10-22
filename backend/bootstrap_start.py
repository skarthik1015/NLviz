from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from export_demo_parquet import export as export_demo_parquet
from seed import seed


def main() -> None:
    backend_root = Path(__file__).resolve().parent
    db_path = backend_root / "data" / "ecommerce.duckdb"

    if not db_path.exists():
        print("DuckDB file not found. Running seed.py before API startup...")
        seed()
    else:
        print(f"DuckDB file found at {db_path}. Skipping seed.")

    # Compose: write the demo tables where jammi-server reads them (a shared volume).
    demo_dir = os.getenv("DEMO_PARQUET_EXPORT_DIR")
    if demo_dir:
        exported = export_demo_parquet(Path(demo_dir))
        print(f"Demo Parquet in {demo_dir}: exported {exported or 'nothing (already present)'}")

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
