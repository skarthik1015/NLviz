"""Development set, database 3: NYC TLC yellow taxi trips for January 2024 (Parquet, ~3M rows) and
the taxi zone lookup (CSV). They are uploaded through the app (upload + attach file), so this only
downloads them into --download-dir (an empty directory of its own) and prints their row counts.

    python eval/bench/datasets/load_taxi.py --download-dir <empty dir>
"""
from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import duckdb

FILES = {
    "yellow_tripdata_2024-01.parquet": "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet",
    "taxi_zone_lookup.csv": "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-dir", required=True)
    args = parser.parse_args()
    target = Path(args.download_dir)
    target.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        path = target / name
        if not path.exists():
            urllib.request.urlretrieve(url, path)
        reader = "read_parquet" if name.endswith(".parquet") else "read_csv_auto"
        rows = duckdb.sql(f"SELECT COUNT(*) FROM {reader}('{path.as_posix()}')").fetchone()[0]
        print(f"{name}: {rows} rows, {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
