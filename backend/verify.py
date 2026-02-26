import duckdb

conn = duckdb.connect("data/ecommerce.duckdb")

checks = [
    ("Orders",   "SELECT COUNT(*) FROM orders"),
    ("Revenue",  "SELECT ROUND(SUM(payment_value), 2) FROM order_payments"),
    ("Tables",   "SHOW TABLES"),
]

for label, sql in checks:
    result = conn.execute(sql).fetchall()
    print(f"{label}: {result}")

conn.close()