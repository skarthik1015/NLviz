from __future__ import annotations

from app.models.semantic_intent import SemanticIntent


_METRIC_KEYWORDS = {
    "total_revenue": ["revenue", "sales", "gmv"],
    "order_count": ["orders", "order count", "number of orders"],
    "average_order_value": ["average order value", "aov"],
    "average_review_score": ["review score", "rating"],
    "average_delivery_days": ["delivery time", "delivery days", "shipping time"],
    "cancellation_rate": ["cancellation rate", "cancel rate", "cancelled"],
}

_DIMENSION_KEYWORDS = {
    "customer_state": ["customer state", "state", "region"],
    "seller_state": ["seller state"],
    "product_category": ["category", "product category"],
    "payment_type": ["payment method", "payment type"],
    "order_status": ["status", "order status"],
    "review_score": ["review score"],
}


def map_question_to_intent(question: str) -> SemanticIntent:
    q = question.lower()

    metric = "order_count"
    for metric_name, keywords in _METRIC_KEYWORDS.items():
        if any(keyword in q for keyword in keywords):
            metric = metric_name
            break

    dimensions: list[str] = []
    for dimension_name, keywords in _DIMENSION_KEYWORDS.items():
        if any(keyword in q for keyword in keywords):
            dimensions.append(dimension_name)

    has_time_trend = any(token in q for token in ["over time", "monthly", "trend", "by month"])
    time_dimension = "order_date" if has_time_trend else None
    time_granularity = "month" if has_time_trend else None

    return SemanticIntent(
        metric=metric,
        dimensions=dimensions,
        time_dimension=time_dimension,
        time_granularity=time_granularity,
        limit=100,
    )
