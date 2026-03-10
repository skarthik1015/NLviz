from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from seed import seed


def main() -> None:
    backend_root = Path(__file__).resolve().parent
    db_path = backend_root / "data" / "ecommerce.duckdb"

    if not db_path.exists():
        print("DuckDB file not found. Running seed.py before API startup...")
        seed()
    else:
        print(f"DuckDB file found at {db_path}. Skipping seed.")

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
