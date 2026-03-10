import duckdb
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
DB_PATH = BACKEND_ROOT / "data" / "ecommerce.duckdb"
RAW_PATH = BACKEND_ROOT / "data" / "raw"

def seed():
    # Remove existing DB so this is idempotent
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    loaded_tables: set[str] = set()

    for table_name, filename in tables.items():
        filepath = RAW_PATH / filename
        if not filepath.exists():
            print(f"WARNING: {filename} not found, skipping")
            continue

        conn.execute(f"""
            CREATE TABLE {table_name} AS
            SELECT * FROM read_csv_auto('{filepath.as_posix()}', header=true)
        """)
        loaded_tables.add(table_name)
        result = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
        count = result[0] if result else 0
        print(f"Loaded {table_name}: {count:,} rows")

    # Join English category names onto products
    if {"products", "category_translation"}.issubset(loaded_tables):
        conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS product_category_name_english TEXT")
        conn.execute("""
            UPDATE products
            SET product_category_name_english = ct.product_category_name_english
            FROM category_translation ct
            WHERE products.product_category_name = ct.product_category_name
        """)
        print("\nCategory translation applied to products table.")
    else:
        print("\nSkipping category translation because products or category_translation was not loaded.")

    conn.close()
    print(f"\nDatabase created at: {DB_PATH}")

if __name__ == "__main__":
    seed()
