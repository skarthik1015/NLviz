from __future__ import annotations

import json
from typing import Any

import plotly.graph_objects as go

from app.models.semantic_intent import SemanticIntent

_BRAND_BAR = "#b5532e"
_BRAND_LINE = "#1f6d57"

_BASE_LAYOUT: dict[str, Any] = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": "inherit", "size": 12},
    "margin": {"l": 48, "r": 16, "t": 40, "b": 64},
    "autosize": True,
    "hovermode": "closest",
}


def _x_key(rows: list[dict[str, Any]]) -> str | None:
    """Return the first non-metric column name from a row."""
    if not rows:
        return None
    for key in rows[0]:
        if key != "metric_value":
            return key
    return None


def _to_json_safe(fig: go.Figure) -> dict[str, Any]:
    """Serialize figure via JSON to guarantee all numpy/plotly types are plain Python.

    ``plotly``'s :meth:`go.Figure.to_json` is annotated to return an
    ``Optional[str]`` which can confuse static type checkers.  In practice it
    always returns a string, but to keep mypy happy we coalesce ``None`` to an
    empty object before passing the result to ``json.loads``.
    """
    json_str = fig.to_json() or "{}"
    # ``json.loads`` expects a ``str``; the ``or "{}"`` above ensures we never
    # pass ``None``.
    return json.loads(json_str)


def _bar_chart(rows: list[dict[str, Any]], title: str) -> dict[str, Any]:
    xk = _x_key(rows)
    x = [str(r.get(xk, "")) for r in rows] if xk else []
    y = [r.get("metric_value", 0) for r in rows]
    fig = go.Figure(
        data=[go.Bar(x=x, y=y, marker_color=_BRAND_BAR)],
        layout={**_BASE_LAYOUT, "title": {"text": title}, "xaxis": {"title": xk or ""}, "yaxis": {"title": "Value"}},
    )
    return _to_json_safe(fig)


def _line_chart(rows: list[dict[str, Any]], title: str) -> dict[str, Any]:
    xk = _x_key(rows)
    x = [str(r.get(xk, "")) for r in rows] if xk else []
    y = [r.get("metric_value", 0) for r in rows]
    fig = go.Figure(
        data=[go.Scatter(x=x, y=y, mode="lines+markers", line={"color": _BRAND_LINE, "width": 2})],
        layout={**_BASE_LAYOUT, "title": {"text": title}, "xaxis": {"title": xk or ""}, "yaxis": {"title": "Value"}},
    )
    return _to_json_safe(fig)


def _stat_chart(rows: list[dict[str, Any]], title: str) -> dict[str, Any]:
    value = rows[0].get("metric_value", 0) if rows else 0
    fig = go.Figure(
        data=[go.Indicator(mode="number", value=float(value), title={"text": title}, number={"font": {"size": 64}})],
        layout={**_BASE_LAYOUT, "margin": {"l": 16, "r": 16, "t": 60, "b": 16}},
    )
    return _to_json_safe(fig)


def build_chart_spec(
    chart_type: str,
    rows: list[dict[str, Any]],
    intent: SemanticIntent,
    title: str,
) -> dict[str, Any]:
    if chart_type == "line":
        return _line_chart(rows, title)
    if chart_type == "bar":
        return _bar_chart(rows, title)
    return _stat_chart(rows, title)
