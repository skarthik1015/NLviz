import duckdb
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "ecommerce.duckdb"

conn = duckdb.connect(str(DB_PATH))

checks = [
    ("Orders",   "SELECT COUNT(*) FROM orders"),
    ("Revenue",  "SELECT ROUND(SUM(payment_value), 2) FROM order_payments"),
    ("Tables",   "SHOW TABLES"),
]

for label, sql in checks:
    result = conn.execute(sql).fetchall()
    print(f"{label}: {result}")

conn.close()
