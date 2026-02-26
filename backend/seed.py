import duckdb
import pandas as pd
from pathlib import Path

DB_PATH = Path("data/ecommerce.duckdb")
RAW_PATH = Path("data/raw")

def seed():
    # Remove existing DB so this is idempotent
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = duckdb.connect(str(DB_PATH))

    tables = {
        "orders":         "olist_orders_dataset.csv",
        "order_items":    "olist_order_items_dataset.csv",
        "order_payments": "olist_order_payments_dataset.csv",
        "order_reviews":  "olist_order_reviews_dataset.csv",
        "customers":      "olist_customers_dataset.csv",
        "sellers":        "olist_sellers_dataset.csv",
        "products":       "olist_products_dataset.csv",
        "category_translation": "product_category_name_translation.csv",
    }

    for table_name, filename in tables.items():
        filepath = RAW_PATH / filename
        if not filepath.exists():
            print(f"WARNING: {filename} not found, skipping")
            continue

        conn.execute(f"""
            CREATE TABLE {table_name} AS
            SELECT * FROM read_csv_auto('{filepath.as_posix()}', header=true)
        """)
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        print(f"Loaded {table_name}: {count:,} rows")

    # Join English category names onto products
    conn.execute("""
        ALTER TABLE products ADD COLUMN IF NOT EXISTS product_category_name_english TEXT;
        UPDATE products
        SET product_category_name_english = ct.product_category_name_english
        FROM category_translation ct
        WHERE products.product_category_name = ct.product_category_name
    """)
    print("\nCategory translation applied to products table.")

    conn.close()
    print(f"\nDatabase created at: {DB_PATH}")

if __name__ == "__main__":
    seed()
