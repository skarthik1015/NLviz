from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from app.agent.state import AgentState

if TYPE_CHECKING:
    from app.services.intent_mapper import IntentMapperConfig


def build_explainer_node(config: "IntentMapperConfig") -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        question = state.get("question", "")
        intent = state.get("intent")
        row_count = state.get("row_count", 0)
        rows = state.get("rows", [])
        validation_status = state.get("validation_status", "ok")

        if validation_status == "empty" or row_count == 0:
            explanation = (
                "No results were found for this query. "
                "Try broadening your criteria or rephrasing the question."
            )
        elif config.mode == "llm" and config.provider and config.model:
            explanation = _llm_explanation(question, intent, row_count, rows, config)
        else:
            explanation = _template_explanation(intent, row_count)

        mode = "llm" if config.mode == "llm" else "template"
        return {
            "explanation": explanation,
            "user_trace": ["Summary generated"],
            "debug_trace": [f"Explainer: mode={mode}, row_count={row_count}"],
        }

    return node


def _template_explanation(intent, row_count: int) -> str:
    if intent is None:
        return f"Query returned {row_count} result(s)."
    metric = intent.metric.replace("_", " ")
    if intent.dimensions:
        dims = ", ".join(d.replace("_", " ") for d in intent.dimensions)
        return f"The query found {row_count} result(s) showing {metric} broken down by {dims}."
    if intent.time_dimension:
        return f"The query returned {row_count} time-series point(s) for {metric}."
    return f"The query returned {row_count} result(s) for {metric}."


def _llm_explanation(question: str, intent, row_count: int, rows: list, config: "IntentMapperConfig") -> str:
    try:
        import json as _json
        from app.services.intent_mapper import _build_completion_client  # deferred to avoid circular import
        client = _build_completion_client(config.provider)
        top_rows = rows[:3]
        sample_parts = []
        for row in top_rows:
            vals = list(row.values())
            if len(vals) >= 2:
                sample_parts.append(f"{vals[0]}={vals[-1]}")
        sample = ", ".join(sample_parts) if sample_parts else "no sample"
        user_prompt = (
            f"Question: {question}\n"
            f"Metric: {intent.metric if intent else 'unknown'}\n"
            f"Row count: {row_count}\n"
            f"Sample results: {sample}\n"
            'In 2-3 sentences, explain what this data shows. Return JSON: {"explanation": "your text here"}'
        )
        raw = client.complete_json(
            system_prompt=(
                'You analyse business data query results and provide clear, concise summaries. '
                'Always respond with valid JSON in the format: {"explanation": "your plain text summary"}'
            ),
            user_prompt=user_prompt,
            model=config.model,
            timeout_ms=config.timeout_ms,
        )
        parsed = _json.loads(raw)
        return str(parsed.get("explanation", "")).strip() or _template_explanation(intent, row_count)
    except Exception:
        return _template_explanation(intent, row_count)
