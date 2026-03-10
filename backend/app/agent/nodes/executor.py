from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.connectors.base import DataConnector


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
            "rows": dataframe.to_dict(orient="records"),
            "row_count": row_count,
            "user_trace": [f"Query executed: {row_count} row(s) returned"],
            "debug_trace": [f"Executor: row_count={row_count}, limit={intent.limit}"],
        }

    return node
