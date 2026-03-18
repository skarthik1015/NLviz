from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectionProfile(BaseModel):
    connection_id: str
    display_name: str
    connector_type: Literal["duckdb", "postgres", "athena"]
    created_at: datetime
    owner_id: str | None = None
    status: Literal["active", "archived"] = "active"
    denied_columns: list[str] = Field(default_factory=list)
    query_ready: bool = False


class SecretRef(BaseModel):
    connection_id: str
    store_key: str


class ValidationSummary(BaseModel):
    total_metrics: int
    valid_metrics: int
    broken_metrics: list[str] = Field(default_factory=list)
    total_dimensions: int
    valid_dimensions: int
    broken_dimensions: list[str] = Field(default_factory=list)
    confidence_score: float


class GenerationMetadata(BaseModel):
    llm_provider: str
    llm_model: str
    generation_time_ms: float
    table_count: int
    column_count: int


class SemanticSchemaVersion(BaseModel):
    version_id: str
    connection_id: str
    status: Literal["draft", "validated", "published", "archived"] = "draft"
    created_at: datetime
    schema_path: str
    validation_summary: ValidationSummary | None = None
    generation_metadata: GenerationMetadata | None = None


class GenerationJob(BaseModel):
    job_id: str
    connection_id: str
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    created_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    schema_version_id: str | None = None
    validation_summary: ValidationSummary | None = None


# --- Request / Response models for API ---

class ConnectionTestRequest(BaseModel):
    connector_type: Literal["duckdb", "postgres", "athena"]
    params: dict[str, Any]


class ConnectionTestResponse(BaseModel):
    success: bool
    tables: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class ConnectionCreateRequest(BaseModel):
    connector_type: Literal["duckdb", "postgres", "athena"]
    params: dict[str, Any]
    display_name: str


class ConnectionCreateResponse(BaseModel):
    connection_id: str
    status: str


class GenerateResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    connection_id: str
    status: str
    error: str | None = None
    schema_version_id: str | None = None
    validation_summary: ValidationSummary | None = None


class PublishRequest(BaseModel):
    version_id: str


class PublishResponse(BaseModel):
    status: str
    connection_id: str
    version_id: str
