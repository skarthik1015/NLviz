"""Where conversation state is kept between turns.

With an app database (DATABASE_URL) conversations live in Postgres and survive restarts; without
one (local development, tests) they live in memory. One checkpointer serves every connection:
thread ids carry the user and connection (QueryService), so threads never collide.
"""
from __future__ import annotations

import logging

from langgraph.checkpoint.memory import InMemorySaver

logger = logging.getLogger(__name__)


class Checkpointer:
    def __init__(self, saver, pool=None):
        self.saver = saver
        self._pool = pool

    @property
    def durable(self) -> bool:
        return self._pool is not None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()


def build_checkpointer(database_url: str | None, *, max_connections: int = 8) -> Checkpointer:
    if not database_url:
        return Checkpointer(InMemorySaver())
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_connections,
        # What PostgresSaver expects: autocommit, no server-side prepared statements, dict rows.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    try:
        saver = PostgresSaver(pool)
        saver.setup()  # creates or migrates the checkpoint tables; idempotent
    except Exception:
        pool.close()
        raise
    logger.info("Conversation state is kept in Postgres")
    return Checkpointer(saver, pool)
