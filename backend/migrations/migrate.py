"""Idempotent migration runner.

Usage::

    from app.storage.db_pool import DatabasePool
    from migrations.migrate import run_migrations

    pool = DatabasePool(dsn="postgresql://...")
    run_migrations(pool)

Or run directly::

    python -m migrations.migrate

Each SQL file in this directory is applied exactly once.  Applied files are
recorded in the ``_migrations`` table so restarts are safe.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent


def run_migrations(pool) -> None:
    """Apply all pending *.sql migrations in filename order.

    Idempotent: already-applied files (tracked in ``_migrations``) are skipped.
    Each file is applied in its own transaction; failure rolls back only that
    file and re-raises so the caller can decide whether to abort startup.
    """
    sql_files = sorted(
        p for p in _MIGRATIONS_DIR.glob("*.sql") if not p.name.startswith("_")
    )
    if not sql_files:
        logger.info("No migration files found in %s", _MIGRATIONS_DIR)
        return

    with pool.acquire() as conn:
        _ensure_migrations_table(conn)

    for sql_file in sql_files:
        _apply_migration(pool, sql_file)


def _ensure_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                filename   TEXT        PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def _apply_migration(pool, sql_file: Path) -> None:
    filename = sql_file.name
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM _migrations WHERE filename = %s", (filename,)
            )
            if cur.fetchone():
                logger.debug("Migration %s already applied — skipping", filename)
                return

        sql = sql_file.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO _migrations (filename) VALUES (%s)", (filename,)
            )

    logger.info("Applied migration: %s", filename)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL environment variable is not set")

    # Import here to avoid pulling in the full app when running standalone
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.storage.db_pool import DatabasePool  # noqa: E402

    pool = DatabasePool(dsn=dsn)
    try:
        run_migrations(pool)
        print("Migrations complete.")
    finally:
        pool.close()
