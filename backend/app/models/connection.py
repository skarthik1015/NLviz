from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectionProfile(BaseModel):
    connection_id: str
    display_name: str
    # "upload": a file converted to Parquet and served by Jammi. "duckdb" and "athena" only appear on
    # connections created before Jammi (Phase 1g); they are listed but no longer served.
    connector_type: Literal["duckdb", "postgres", "athena", "upload", "mysql"]
    created_at: datetime
    owner_id: str | None = None
    status: Literal["active", "archived"] = "active"
    denied_columns: list[str] = Field(default_factory=list)
    query_ready: bool = False
    # Query engine. "jammi": the connection's data is registered as Jammi sources and queried
    # through jammi-server; "legacy": the v1 direct connectors (removed at Phase 1 gate 1g).
    engine: Literal["legacy", "jammi"] = "legacy"
    # Isolation unit (decision A1). One personal workspace per user for now: the owner's id.
    workspace_id: str | None = None
    # Logical table name -> physical Jammi reference "<source>.public.<table>" (engine="jammi").
    jammi_tables: dict[str, str] = Field(default_factory=dict)
    # Where answers come from, kept so every later session can say it (not only the create
    # response): "live" queried in place, "snapshot" copied at copied_at. `notes` say why.
    mode: Literal["live", "snapshot"] = "live"
    copied_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


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
    # Time dimensions are probed too (Phase 3e); defaults keep older stored summaries readable.
    total_time_dimensions: int = 0
    valid_time_dimensions: int = 0
    broken_time_dimensions: list[str] = Field(default_factory=list)
    # Filter-only columns (row conditions) are probed too; defaults keep older summaries readable.
    total_filter_columns: int = 0
    broken_filter_columns: list[str] = Field(default_factory=list)
    confidence_score: float
    # What was published after the broken items were removed. The counts above describe the
    # proposal (what the LLM and completion produced, and what of it ran); these describe the model
    # that answers questions (review: the UI showed the proposal's figures as the published model's).
    published_metrics: int | None = None
    published_dimensions: int | None = None
    published_time_dimensions: int | None = None
    published_filter_columns: int | None = None


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
    connector_type: Literal["postgres", "mysql"]
    params: dict[str, Any]


class ConnectionTestResponse(BaseModel):
    success: bool
    tables: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class ConnectionCreateRequest(BaseModel):
    connector_type: Literal["postgres", "mysql"]
    params: dict[str, Any]
    display_name: str


class S3ImportRequestBody(BaseModel):
    """Import one CSV/Parquet object. Credentials are used once and never stored."""

    s3_url: str
    display_name: str
    region: str = "us-east-1"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    endpoint_url: str | None = None


class AttachFileResponse(BaseModel):
    """A file added to an existing connection: one more table, joinable with the others."""

    connection_id: str
    table: str
    renamed_columns: dict[str, str] = Field(default_factory=dict)
    # The published schema does not know the new table until it is generated again.
    schema_regeneration_required: bool = True


class ConnectionCreateResponse(BaseModel):
    connection_id: str
    status: str
    # Tables that could not be registered (e.g. mixed-case names Jammi cannot address unquoted).
    skipped_tables: list[str] = Field(default_factory=list)
    # Uploads: original column name -> the lowercase identifier it is queried by.
    renamed_columns: dict[str, str] = Field(default_factory=dict)
    # "live": queried in place; "snapshot": copied when connected (MySQL, or a Postgres database
    # whose column types the engine cannot read live). `notes` say why, for the user.
    mode: str = "live"
    notes: list[str] = Field(default_factory=list)


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
