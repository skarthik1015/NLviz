from typing import TYPE_CHECKING

from .feedback_store import FeedbackStore
from .intent_mapper import (
    HeuristicIntentMapper,
    IntentMapper,
    IntentMapperConfig,
    IntentMapperRouter,
    IntentMappingError,
    IntentValidationError,
    LLMIntentMapper,
    validate_semantic_intent,
)

if TYPE_CHECKING:
    from .query_service import QueryService


def __getattr__(name: str):
    if name == "QueryService":
        from .query_service import QueryService

        return QueryService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "FeedbackStore",
    "HeuristicIntentMapper",
    "IntentMapper",
    "IntentMapperConfig",
    "IntentMapperRouter",
    "IntentMappingError",
    "IntentValidationError",
    "LLMIntentMapper",
    "QueryService",
    "validate_semantic_intent",
]
