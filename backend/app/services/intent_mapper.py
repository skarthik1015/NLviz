from __future__ import annotations

import re

from app.models.semantic_intent import SemanticIntent


_METRIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("average_order_value", ("average order value", "aov")),
    ("average_review_score", ("review score", "rating")),
    ("average_delivery_days", ("delivery time", "delivery days", "shipping time")),
    ("cancellation_rate", ("cancellation rate", "cancel rate", "cancelled")),
    ("total_revenue", ("revenue", "sales", "gmv")),
    ("order_count", ("orders", "order count", "number of orders")),
)

_DIMENSION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("seller_state", ("seller state",)),
    ("customer_state", ("customer state", "state", "region")),
    ("product_category", ("product category", "category")),
    ("payment_type", ("payment method", "payment type")),
    ("order_status", ("order status", "status")),
    ("review_score", ("review score",)),
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

        # Avoid treating "seller state" as both seller_state and customer_state via the generic "state" token.
        if dimension_name == "customer_state" and seller_state_matched and not _contains_phrase(question, "customer state"):
            continue

        if dimension_name == "seller_state":
            seller_state_matched = True

        dimensions.append(dimension_name)

    return dimensions


def map_question_to_intent(question: str) -> SemanticIntent:
    q = question.lower()

    metric = _find_metric(q)
    dimensions = _find_dimensions(q)

    has_time_trend = any(token in q for token in ["over time", "monthly", "trend", "by month"])
    time_dimension = "order_date" if has_time_trend else None
    time_granularity = "month" if has_time_trend else None
    order_by = "time_asc" if has_time_trend else "metric_desc"

    return SemanticIntent(
        metric=metric,
        dimensions=dimensions,
        time_dimension=time_dimension,
        time_granularity=time_granularity,
        order_by=order_by,
        limit=100,
    )
