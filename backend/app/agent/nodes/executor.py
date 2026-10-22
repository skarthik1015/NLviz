from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import pandas as pd

from app.agent.state import MAX_REPAIRS, AgentState
from app.connectors.base import DataConnector
from app.connectors.jammi_connector import CrossSourceQueryError
from app.security import SQLSafetyError

# Refusals are answers, not mistakes the LLM can repair.
_NOT_REPAIRABLE = (SQLSafetyError, CrossSourceQueryError)


def _sanitize_value(value: Any) -> Any:
    """Convert non-JSON-serializable types (e.g. memoryview, bytes) to safe representations."""
    if isinstance(value, memoryview):
        return bytes(value).hex()
    if isinstance(value, bytes):
        return value.hex()
    # Missing values: pandas turns a NULL timestamp into NaT, which JSON cannot encode (a 500 on
    # "orders by delivery month", where undelivered orders form a NULL bucket). NaN/NA likewise.
    if value is pd.NaT or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return None
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

        try:
            dataframe = connector.execute_query(sql, limit=intent.limit)
        except _NOT_REPAIRABLE:
            raise
        except Exception as exc:
            repairs = state.get("repair_count", 0)
            # Only an LLM intent can be reinterpreted; explicit and heuristic intents fail as before.
            if state.get("intent_source") != "llm" or repairs >= MAX_REPAIRS:
                raise
            error = str(exc).splitlines()[0][:500] if str(exc) else type(exc).__name__
            intent_json = json.dumps(intent.model_dump(mode="json", exclude_none=True), separators=(",", ":"))
            return {
                "execution_error": error,
                "repair_feedback": (
                    f"The query for your previous answer {intent_json} failed on the engine: {error}. "
                    "Answer again with a different valid interpretation of the question."
                ),
                "repair_count": repairs + 1,
                "rows": [],
                "row_count": 0,
                "user_trace": [f"The query failed; reinterpreting the question (attempt {repairs + 1})"],
                "debug_trace": [f"Executor: error {error}; repair {repairs + 1}/{MAX_REPAIRS}"],
                "audit": [{"stage": "execution_error", "attempt": repairs, "error": error, "sql": sql}],
            }
        row_count = len(dataframe.index)
        return {
            "rows": _sanitize_rows(dataframe.to_dict(orient="records")),
            "row_count": row_count,
            "execution_error": None,
            "user_trace": [f"Query executed: {row_count} row(s) returned"],
            "debug_trace": [f"Executor: row_count={row_count}, limit={intent.limit}"],
        }

    return node


def route_after_execute(state: AgentState) -> str:
    return "repair" if state.get("execution_error") else "validate"
