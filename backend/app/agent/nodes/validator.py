from __future__ import annotations

from collections.abc import Callable

from app.agent.state import AgentState

_MAX_RETRIES = 2
_CHART_ROW_LIMIT = 500


def build_validator_node() -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        row_count = state.get("row_count", 0)
        intent = state.get("intent")
        retry_count = state.get("retry_count", 0)

        # Case 1: empty result — retry if fixable (date filters are present)
        if row_count == 0:
            has_dates = intent is not None and (intent.start_date or intent.end_date)
            if has_dates and retry_count < _MAX_RETRIES:
                return {
                    "validation_status": "failed",
                    "validation_errors": ["Query returned 0 rows"],
                    "correction_hint": "remove_date_filters",
                    "retry_count": retry_count + 1,
                    "user_trace": [
                        f"No results found — retrying without date filters (attempt {retry_count + 1})"
                    ],
                    "debug_trace": [
                        f"Validator: 0 rows with date filters, retry {retry_count + 1}/{_MAX_RETRIES}"
                    ],
                }
            return {
                "validation_status": "empty",
                "validation_errors": ["Query returned 0 rows"],
                "correction_hint": None,
                "user_trace": ["Query returned no results."],
                "debug_trace": [f"Validator: empty result, retry_count={retry_count}"],
            }

        # Case 2: too many rows for a chart intent — retry with tighter limit
        is_chart_intent = intent is not None and len(intent.dimensions) > 0
        if row_count > _CHART_ROW_LIMIT and is_chart_intent:
            if retry_count < _MAX_RETRIES:
                return {
                    "validation_status": "failed",
                    "validation_errors": [f"Result too large for chart: {row_count} rows"],
                    "correction_hint": f"reduce_limit:{_CHART_ROW_LIMIT}",
                    "retry_count": retry_count + 1,
                    "user_trace": [
                        f"Result too large ({row_count} rows) — retrying with limit {_CHART_ROW_LIMIT}"
                    ],
                    "debug_trace": [
                        f"Validator: {row_count} > {_CHART_ROW_LIMIT} chart limit, retry {retry_count + 1}/{_MAX_RETRIES}"
                    ],
                }
            # Retries exhausted — truncate in place
            return {
                "validation_status": "truncated",
                "validation_errors": [],
                "rows": state.get("rows", [])[:_CHART_ROW_LIMIT],
                "row_count": _CHART_ROW_LIMIT,
                "correction_hint": None,
                "user_trace": [f"Result truncated to {_CHART_ROW_LIMIT} rows for rendering."],
                "debug_trace": [f"Validator: truncated to {_CHART_ROW_LIMIT}, retries exhausted"],
            }

        # Case 3: all good
        return {
            "validation_status": "ok",
            "validation_errors": [],
            "correction_hint": None,
            "user_trace": [f"Validated: {row_count} rows"],
            "debug_trace": [f"Validator: ok, row_count={row_count}"],
        }

    return node


def route_after_validator(state: AgentState) -> str:
    """LangGraph conditional edge: route to sql_builder on retry, chart_selector otherwise."""
    if state.get("validation_status") == "failed":
        return "retry"
    return "continue"
