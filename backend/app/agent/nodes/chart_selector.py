from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState
from app.charts import build_chart_spec, select_chart_type


def build_chart_selector_node() -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        intent = state.get("intent")
        rows = state.get("rows", [])
        row_count = state.get("row_count", 0)

        if intent is None or row_count == 0:
            return {
                "chart_spec": None,
                "user_trace": ["No chart generated — query returned no data."],
                "debug_trace": [f"Chart selector: skipped, intent={intent is not None}, row_count={row_count}"],
            }

        chart_type = select_chart_type(intent, row_count)
        title = _build_title(intent)
        chart_spec = build_chart_spec(chart_type, rows, intent, title)

        return {
            "chart_spec": chart_spec,
            "user_trace": [f"Chart generated: {chart_type} ({row_count} rows)"],
            "debug_trace": [f"Chart selector: chart_type={chart_type}, row_count={row_count}"],
        }

    return node


def _build_title(intent) -> str:
    parts = [intent.metric.replace("_", " ").title()]
    if intent.dimensions:
        parts.append("by " + ", ".join(d.replace("_", " ") for d in intent.dimensions))
    if intent.time_dimension:
        parts.append("over time")
    return " ".join(parts)
