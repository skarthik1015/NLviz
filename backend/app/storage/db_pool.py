"""PostgreSQL connection pool.

Wraps psycopg2's ThreadedConnectionPool in a context-manager interface so
connections are always properly returned to the pool, even on exceptions.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)


class DatabasePool:
    """Thread-safe PostgreSQL connection pool backed by psycopg2.

    Usage::

        pool = DatabasePool("postgresql://user:pass@host/db")
        with pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    The pool is created lazily on first use to avoid blocking at import time.
    """

    def __init__(self, dsn: str, minconn: int = 1, maxconn: int = 10) -> None:
        self._dsn = dsn
        self._minconn = minconn
        self._maxconn = maxconn
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None

    def _get_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        if self._pool is None:
            logger.info("Initialising PostgreSQL connection pool (min=%d max=%d)", self._minconn, self._maxconn)
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                self._minconn,
                self._maxconn,
                dsn=self._dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
        return self._pool

    @contextmanager
    def acquire(self) -> Iterator[psycopg2.extensions.connection]:
        """Acquire a connection, yield it, then return it to the pool.

        If the connection is in a failed transaction state it is rolled back
        before being returned so subsequent users get a clean state.
        """
        pool = self._get_pool()
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    def close(self) -> None:
        """Close all connections in the pool (call at application shutdown)."""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
            logger.info("PostgreSQL connection pool closed")
