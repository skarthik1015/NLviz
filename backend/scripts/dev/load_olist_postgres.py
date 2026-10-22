"""Load the bundled Olist dataset into a dev/test Postgres (public schema), for live Jammi tests.

Also creates analytics.daily_orders (a non-public schema, to check issue J24) and an empty
database nlviz_app for the RDS connection-store test.

    python scripts/dev/load_olist_postgres.py "host=127.0.0.1 port=15432 dbname=olist user=postgres password=..."
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import duckdb
import psycopg2

DUCKDB_PATH = Path(__file__).resolve().parents[2] / "data" / "ecommerce.duckdb"
PG_TYPES = {"VARCHAR": "text", "BIGINT": "bigint", "INTEGER": "integer", "DOUBLE": "double precision",
            "TIMESTAMP": "timestamp", "DATE": "date", "BOOLEAN": "boolean", "FLOAT": "real"}


def main(dsn: str) -> None:
    duck = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    pg = psycopg2.connect(dsn)
    pg.autocommit = True
    with pg.cursor() as cur:
        tables = [r[0] for r in duck.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' ORDER BY 1").fetchall()]
        for table in tables:
            cols = duck.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ? ORDER BY column_index",
                [table]).fetchall()
            ddl = ", ".join(f'"{c}" {PG_TYPES[t]}' for c, t in cols)
            cur.execute(f'DROP TABLE IF EXISTS public."{table}"')
            cur.execute(f'CREATE TABLE public."{table}" ({ddl})')
            buf = io.StringIO()
            duck.execute(f'SELECT * FROM "{table}"').df().to_csv(buf, index=False, header=False)
            buf.seek(0)
            cur.copy_expert(f'COPY public."{table}" FROM STDIN WITH (FORMAT csv)', buf)
            cur.execute(f'SELECT COUNT(*) FROM public."{table}"')
            print(f"{table}: {cur.fetchone()[0]} rows")
        cur.execute("CREATE SCHEMA IF NOT EXISTS analytics")
        cur.execute("DROP TABLE IF EXISTS analytics.daily_orders")
        cur.execute("CREATE TABLE analytics.daily_orders AS SELECT date_trunc('day', order_purchase_timestamp) AS day, "
                    "COUNT(*) AS n FROM public.orders GROUP BY 1")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'nlviz_app'")
        if cur.fetchone() is None:
            cur.execute("CREATE DATABASE nlviz_app")
    print("loaded")


if __name__ == "__main__":
    main(sys.argv[1])
