from __future__ import annotations

import re

from app.models.semantic_intent import SemanticIntent


# Order matters: more specific phrases before shorter ones that could false-match.
_METRIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("average_order_value", ("average order value", "aov", "avg order value", "mean order value")),
    ("average_review_score", ("review score", "average review", "avg review", "rating", "ratings", "customer satisfaction")),
    ("average_delivery_days", ("delivery time", "delivery days", "shipping time", "how long to deliver", "days to deliver")),
    ("cancellation_rate", ("cancellation rate", "cancel rate", "cancellation", "cancelled orders", "cancellations")),
    ("total_revenue", ("revenue", "sales", "gmv", "total sales", "income", "earnings")),
    # order_count intentionally last — "orders" is a short token that can match many phrases
    ("order_count", ("order count", "number of orders", "orders placed", "how many orders", "orders")),
)

_DIMENSION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # seller_state must be checked before customer_state to guard the "state" fallback below
    ("seller_state", ("seller state", "by seller", "per seller location")),
    ("customer_state", ("customer state", "by state", "by region", "per state", "state", "region")),
    ("product_category", ("product category", "by category", "per category", "category", "categories", "product type", "item type")),
    ("payment_type", ("payment method", "payment type", "by payment", "how paid")),
    ("order_status", ("order status", "by status", "status")),
    ("review_score", ("by review score", "by rating")),
)


def _contains_phrase(question: str, phrase: str) -> bool:
    escaped = re.escape(phrase)
    return re.search(rf"(?<!\w){escaped}(?!\w)", question) is not None


def _find_metric(question: str) -> str:
    for metric_name, keywords in _METRIC_KEYWORDS:
        if any(_contains_phrase(question, keyword) for keyword in keywords):
            return metric_name
    return "order_count"


def _find_dimensions(question: str) -> list[str]:
    dimensions: list[str] = []
    seller_state_matched = False

    for dimension_name, keywords in _DIMENSION_KEYWORDS:
        matched = any(_contains_phrase(question, keyword) for keyword in keywords)
        if not matched:
            continue

        # Avoid treating "seller state" as both seller_state AND customer_state via "state" token.
        if dimension_name == "customer_state" and seller_state_matched and not _contains_phrase(question, "customer state"):
            continue

        if dimension_name == "seller_state":
            seller_state_matched = True

        dimensions.append(dimension_name)

    return dimensions


def _find_top_n(question: str) -> int | None:
    """Detect 'top N', 'bottom N', 'first N' and return N as the limit."""
    match = re.search(r"\btop\s+(\d+)\b|\bbottom\s+(\d+)\b|\bfirst\s+(\d+)\b", question)
    if match:
        value = match.group(1) or match.group(2) or match.group(3)
        n = int(value)
        return min(n, 500)
    return None


def _find_time_range(question: str) -> tuple[str | None, str | None]:
    """Return (start_date, end_date) ISO strings if a year is mentioned."""
    year_match = re.search(r"\b(201[5-9]|202[0-9])\b", question)
    if year_match:
        year = year_match.group(1)
        return f"{year}-01-01", f"{year}-12-31"
    return None, None


def map_question_to_intent(question: str) -> SemanticIntent:
    q = question.lower()

    metric = _find_metric(q)
    dimensions = _find_dimensions(q)

    has_time_trend = any(token in q for token in [
        "over time", "monthly", "trend", "by month", "by week", "per month",
        "each month", "week by week", "by quarter", "quarterly",
    ])
    time_dimension = "order_date" if has_time_trend else None
    time_granularity = "month" if has_time_trend else None
    order_by: str = "time_asc" if has_time_trend else "metric_desc"

    top_n = _find_top_n(q)
    start_date, end_date = _find_time_range(q)

    limit = top_n if top_n is not None else 100

    return SemanticIntent(
        metric=metric,
        dimensions=dimensions,
        time_dimension=time_dimension,
        time_granularity=time_granularity,
        order_by=order_by,
        limit=limit,
        start_date=start_date,
        end_date=end_date,
    )