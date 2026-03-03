from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .semantic_intent import SemanticIntent


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    intent: SemanticIntent | None = None


class ChatResponse(BaseModel):
    query_id: str
    question: str
    intent: SemanticIntent
    intent_source: Literal["heuristic", "llm", "llm_fallback", "explicit"]
    sql: str
    rows: list[dict[str, Any]]
    row_count: int
    trace: list[str]


class SchemaResponse(BaseModel):
    connector_type: str
    tables: dict[str, list[dict[str, Any]]]
    row_counts: dict[str, int]
    join_paths: list[dict[str, str]]
    metrics: list[dict[str, str]]
    dimensions: list[dict[str, str]]
    time_dimensions: list[dict[str, str]]


class FeedbackRequest(BaseModel):
    query_id: str
    rating: Literal["positive", "negative"]
    comment: str | None = None
    idempotency_key: str | None = None


class FeedbackRecord(BaseModel):
    feedback_id: str
    query_id: str
    rating: Literal["positive", "negative"]
    comment: str | None = None
    created_at: datetime
    idempotency_key: str | None = None


class FeedbackResponse(BaseModel):
    status: Literal["created", "updated"]
    feedback: FeedbackRecord
