"""Load the bundled Olist Parquet files into a dev/test MySQL, for live Jammi tests.

    python scripts/dev/load_olist_mysql.py 127.0.0.1 13306 root <password>

Creates database `olist` with the 8 tables (types mapped from Arrow), plus a view `v_orders` and a
mixed-case table `AuditLog` to check how Jammi's MySQL source discovers them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pymysql

PARQUET_DIR = Path(__file__).resolve().parents[2] / "data" / "parquet" / "olist"
BATCH = 5000


def mysql_type(arrow_type: pa.DataType) -> str:
    if pa.types.is_integer(arrow_type):
        return "BIGINT"
    if pa.types.is_floating(arrow_type):
        return "DOUBLE"
    if pa.types.is_boolean(arrow_type):
        return "BOOLEAN"
    if pa.types.is_timestamp(arrow_type):
        return "DATETIME(6)"
    if pa.types.is_date(arrow_type):
        return "DATE"
    return "TEXT"


def main(host: str, port: int, user: str, password: str) -> None:
    conn = pymysql.connect(host=host, port=port, user=user, password=password, autocommit=True, charset="utf8mb4")
    with conn.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS olist CHARACTER SET utf8mb4")
        cur.execute("USE olist")
        for path in sorted(PARQUET_DIR.glob("*.parquet")):
            table = pq.read_table(path)
            name = path.stem
            ddl = ", ".join(f"`{f.name}` {mysql_type(f.type)}" for f in table.schema)
            cur.execute(f"DROP TABLE IF EXISTS `{name}`")
            cur.execute(f"CREATE TABLE `{name}` ({ddl})")
            placeholders = ", ".join(["%s"] * table.num_columns)
            rows = [tuple(r.values()) for r in table.to_pylist()]
            for start in range(0, len(rows), BATCH):
                cur.executemany(f"INSERT INTO `{name}` VALUES ({placeholders})", rows[start:start + BATCH])
            cur.execute(f"SELECT COUNT(*) FROM `{name}`")
            print(f"{name}: {cur.fetchone()[0]} rows")
        cur.execute("CREATE OR REPLACE VIEW v_orders AS SELECT order_id, order_status FROM orders")
        cur.execute("DROP TABLE IF EXISTS `AuditLog`")
        cur.execute("CREATE TABLE `AuditLog` (id BIGINT)")
    print("loaded")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
