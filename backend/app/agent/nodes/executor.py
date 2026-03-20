from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent.state import AgentState
from app.connectors.base import DataConnector


def _sanitize_value(value: Any) -> Any:
    """Convert non-JSON-serializable types (e.g. memoryview, bytes) to safe representations."""
    if isinstance(value, memoryview):
        return bytes(value).hex()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _sanitize_rows(rows: list[dict]) -> list[dict]:
    return [{k: _sanitize_value(v) for k, v in row.items()} for row in rows]


def build_executor_node(connector: DataConnector) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        sql = state.get("sql")
        if not sql:
            raise ValueError("SQL must be present before execution")

        intent = state.get("intent")
        if intent is None:
            raise ValueError("Intent must be present before execution")

        dataframe = connector.execute_query(sql, limit=intent.limit)
        row_count = len(dataframe.index)
        return {
            "rows": _sanitize_rows(dataframe.to_dict(orient="records")),
            "row_count": row_count,
            "user_trace": [f"Query executed: {row_count} row(s) returned"],
            "debug_trace": [f"Executor: row_count={row_count}, limit={intent.limit}"],
        }

    return node
