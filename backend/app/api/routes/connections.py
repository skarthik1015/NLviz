"""Connection management endpoints: test, create, upload, generate, publish, delete."""
from __future__ import annotations

import logging
import os
import re as _re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

import asyncio

import duckdb
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request

from app.connectors import DuckDBConnector
from app.connectors.postgres_connector import PostgresConnector
from app.dependencies import (
    get_audit_log,
    get_connection_service,
    get_connection_store,
    get_job_manager,
    get_schema_storage,
    get_secret_store,
    get_upload_storage,
)
from app.security.auth import AuthUser, get_current_user
from app.models.connection import (
    ConnectionCreateRequest,
    ConnectionCreateResponse,
    ConnectionProfile,
    ConnectionTestRequest,
    ConnectionTestResponse,
    GenerateResponse,
    JobStatusResponse,
    PublishRequest,
    PublishResponse,
)
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionService
from app.services.connection_store import ConnectionStore
from app.services.generation_job_manager import GenerationJobManager
from app.services.secret_store import SecretStore
from app.storage.schema_storage import BaseSchemaStorage
from app.storage.s3_storage import S3Storage

router = APIRouter(prefix="/connections", tags=["connections"])

_UPLOADS_DIR = Path(__file__).resolve().parents[3] / "data" / "uploads"
_MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "100"))
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB


# ── GET /connections ─────────────────────────────────────────────────


@router.get("")
async def list_connections(
    user: AuthUser = Depends(get_current_user),
    store: ConnectionStore = Depends(get_connection_store),
) -> list[dict]:
    profiles = store.list_connections(owner_id=user.user_id)
    published_ids = store.get_published_connection_ids()
    return [
        p.model_copy(update={"query_ready": p.connection_id in published_ids})
         .model_dump(exclude={"denied_columns"})
        for p in profiles
    ]


# ── POST /connections/test ───────────────────────────────────────────


@router.post("/test", response_model=ConnectionTestResponse)
async def test_connection(
    request: ConnectionTestRequest,
    user: AuthUser = Depends(get_current_user),
    audit: AuditLog = Depends(get_audit_log),
) -> ConnectionTestResponse:
    audit.log("connection.test", metadata={"connector_type": request.connector_type})

    try:
        connector = _build_connector(request.connector_type, request.params)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Connection test failed during connector init")
        return ConnectionTestResponse(success=False, error=f"CONNECTOR_BUILD_FAILED: {exc}")

    try:
        connection_ok = connector.test_connection()
    except Exception as exc:
        msg = str(exc)
        if "password authentication" in msg or "authentication failed" in msg:
            return ConnectionTestResponse(success=False, error="AUTH_FAILED: wrong username or password")
        if "could not connect to server" in msg or "Connection refused" in msg:
            return ConnectionTestResponse(success=False, error="NETWORK_UNREACHABLE: cannot reach host — check host and port")
        if "does not exist" in msg:
            return ConnectionTestResponse(success=False, error=f"DATABASE_NOT_FOUND: {msg}")
        if "timeout" in msg.lower():
            return ConnectionTestResponse(success=False, error="TIMEOUT: connection timed out — check host and port")
        return ConnectionTestResponse(success=False, error=f"CONNECTION_TEST_FAILED: {msg}")
    if connection_ok is False:
        connector.close()
        return ConnectionTestResponse(success=False, error="CONNECTION_TEST_FAILED")

    try:
        schema_ctx = connector.get_schema()
        tables = [
            {
                "name": name,
                "row_count": schema_ctx.row_counts.get(name, 0),
                "column_count": len(cols),
            }
            for name, cols in schema_ctx.tables.items()
        ]
        return ConnectionTestResponse(success=True, tables=tables)
    except Exception as exc:
        logger.exception("Connection test failed during schema introspection")
        return ConnectionTestResponse(success=False, error=f"SCHEMA_INTROSPECTION_FAILED: {exc}")
    finally:
        connector.close()


# ── POST /connections ────────────────────────────────────────────────


@router.post("", response_model=ConnectionCreateResponse)
async def create_connection(
    request: ConnectionCreateRequest,
    user: AuthUser = Depends(get_current_user),
    store: ConnectionStore = Depends(get_connection_store),
    secrets: SecretStore = Depends(get_secret_store),
    audit: AuditLog = Depends(get_audit_log),
) -> ConnectionCreateResponse:
    connection_id = str(uuid4())

    # Store credentials
    secrets.put(connection_id, request.params)

    profile = ConnectionProfile(
        connection_id=connection_id,
        display_name=request.display_name,
        connector_type=request.connector_type,
        created_at=datetime.now(timezone.utc),
        owner_id=user.user_id,
    )
    store.create_connection(profile)
    audit.log("connection.create", connection_id=connection_id)

    return ConnectionCreateResponse(connection_id=connection_id, status="active")


# ── POST /connections/upload ─────────────────────────────────────────


@router.post("/upload", response_model=ConnectionCreateResponse)
async def upload_file(
    file: UploadFile = File(...),
    display_name: str = Form(...),
    user: AuthUser = Depends(get_current_user),
    store: ConnectionStore = Depends(get_connection_store),
    secrets: SecretStore = Depends(get_secret_store),
    audit: AuditLog = Depends(get_audit_log),
    s3: S3Storage | None = Depends(get_upload_storage),
) -> ConnectionCreateResponse:
    if file.filename is None:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension before reading payload bytes.
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".csv", ".parquet"):
        raise HTTPException(status_code=400, detail="Only CSV and Parquet files are supported")

    table_name = _sanitize_table_name(Path(file.filename).stem)
    connection_id = str(uuid4())

    max_bytes = _MAX_UPLOAD_MB * 1024 * 1024
    tmp_path: str | None = None
    try:
        tmp_path, bytes_written = await _stream_upload_to_temp(
            upload=file,
            suffix=suffix,
            max_bytes=max_bytes,
        )
        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        if s3 is not None:
            # ── S3 path: upload raw file; DuckDB reads via httpfs ───────
            s3_key = f"uploads/{connection_id}/{file.filename}"
            with open(tmp_path, "rb") as fobj:
                db_path_str = await asyncio.to_thread(s3.upload_fileobj, s3_key, fobj)
        else:
            # ── Local path: import into a local .duckdb file ────────────
            _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            local_db = _UPLOADS_DIR / f"{connection_id}.duckdb"
            conn = duckdb.connect(str(local_db))
            try:
                if suffix == ".csv":
                    conn.execute(
                        f"CREATE TABLE {_quote_identifier(table_name)} AS SELECT * FROM read_csv_auto(?)",
                        [tmp_path],
                    )
                else:
                    conn.execute(
                        f"CREATE TABLE {_quote_identifier(table_name)} AS SELECT * FROM read_parquet(?)",
                        [tmp_path],
                    )
            finally:
                conn.close()
            db_path_str = str(local_db)
    finally:
        await file.close()
        if tmp_path and Path(tmp_path).exists():
            os.unlink(tmp_path)

    # Store connection params (table_name needed for S3 httpfs VIEW creation)
    params: dict = {"db_path": db_path_str}
    if s3 is not None:
        params["table_name"] = table_name
    secrets.put(connection_id, params)

    profile = ConnectionProfile(
        connection_id=connection_id,
        display_name=display_name,
        connector_type="duckdb",
        created_at=datetime.now(timezone.utc),
        owner_id=user.user_id,
    )
    store.create_connection(profile)
    audit.log("connection.create", connection_id=connection_id, metadata={"source": "upload"})

    return ConnectionCreateResponse(connection_id=connection_id, status="active")


# ── POST /connections/{id}/generate ──────────────────────────────────


@router.post("/{connection_id}/generate", response_model=GenerateResponse)
async def generate_schema(
    connection_id: str,
    user: AuthUser = Depends(get_current_user),
    store: ConnectionStore = Depends(get_connection_store),
    secrets: SecretStore = Depends(get_secret_store),
    job_mgr: GenerationJobManager = Depends(get_job_manager),
    audit: AuditLog = Depends(get_audit_log),
) -> GenerateResponse:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    _verify_owner(profile, user)
    if profile.status != "active":
        raise HTTPException(status_code=400, detail="Connection is archived")

    try:
        connector = _build_connector_from_profile(profile, secrets)
    except Exception:
        logger.exception("Failed to build connector for generation job on connection %s", connection_id)
        raise HTTPException(status_code=500, detail="SCHEMA_GENERATION_FAILED")

    audit.log("schema.generate_start", connection_id=connection_id)

    job = job_mgr.start_job(connection_id=connection_id, connector=connector)
    return GenerateResponse(job_id=job.job_id, status=job.status)


# ── GET /connections/{id}/jobs/{job_id} ──────────────────────────────


@router.get("/{connection_id}/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    connection_id: str,
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    job_mgr: GenerationJobManager = Depends(get_job_manager),
    store: ConnectionStore = Depends(get_connection_store),
) -> JobStatusResponse:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    _verify_owner(profile, user)

    job = job_mgr.get_job(job_id)
    if job is None or job.connection_id != connection_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=job.job_id,
        connection_id=job.connection_id,
        status=job.status,
        error=job.error,
        schema_version_id=job.schema_version_id,
        validation_summary=job.validation_summary,
    )


# ── POST /connections/{id}/publish ───────────────────────────────────


@router.post("/{connection_id}/publish", response_model=PublishResponse)
async def publish_schema(
    connection_id: str,
    request: PublishRequest,
    user: AuthUser = Depends(get_current_user),
    conn_service: ConnectionService = Depends(get_connection_service),
    store: ConnectionStore = Depends(get_connection_store),
    audit: AuditLog = Depends(get_audit_log),
) -> PublishResponse:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    _verify_owner(profile, user)

    version = store.get_version(request.version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Schema version not found")
    if version.connection_id != connection_id:
        raise HTTPException(status_code=400, detail="Version does not belong to this connection")
    if version.status != "validated":
        raise HTTPException(status_code=400, detail=f"Cannot publish version in status '{version.status}'")
    if version.validation_summary and version.validation_summary.confidence_score < 0.3:
        raise HTTPException(status_code=400, detail="Schema confidence too low to publish")

    try:
        conn_service.activate_schema(connection_id, request.version_id)
    except ValueError as exc:
        logger.exception("Failed to activate schema for connection %s", connection_id)
        raise HTTPException(status_code=400, detail="PUBLISH_FAILED") from exc
    except Exception:
        logger.exception("Unexpected error activating schema for connection %s", connection_id)
        raise HTTPException(status_code=500, detail="PUBLISH_FAILED")

    audit.log("schema.publish", connection_id=connection_id, metadata={"version_id": request.version_id})

    return PublishResponse(
        status="published",
        connection_id=connection_id,
        version_id=request.version_id,
    )


# ── DELETE /connections/{id} ─────────────────────────────────────────


@router.delete("/{connection_id}")
async def delete_connection(
    connection_id: str,
    user: AuthUser = Depends(get_current_user),
    store: ConnectionStore = Depends(get_connection_store),
    secrets: SecretStore = Depends(get_secret_store),
    conn_service: ConnectionService = Depends(get_connection_service),
    audit: AuditLog = Depends(get_audit_log),
    s3: S3Storage | None = Depends(get_upload_storage),
    schema_storage: BaseSchemaStorage | None = Depends(get_schema_storage),
) -> dict:
    if connection_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default connection")

    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    _verify_owner(profile, user)

    params = secrets.get_by_connection_id(connection_id)
    versions = store.list_versions(connection_id)

    store.archive_connection(connection_id)
    store.archive_versions_for_connection(connection_id)
    _cleanup_uploaded_artifact(profile=profile, params=params, s3=s3)
    _cleanup_schema_artifacts(versions=versions, schema_storage=schema_storage)
    secrets.delete(connection_id)
    conn_service.evict_runtime(connection_id)

    audit.log("connection.delete", connection_id=connection_id)
    return {"status": "archived", "connection_id": connection_id}


# ── Auth helpers ─────────────────────────────────────────────────


def _verify_owner(profile: ConnectionProfile, user: AuthUser) -> None:
    """Raise 403 if the connection belongs to a different user."""
    if profile.owner_id is not None and profile.owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="Access denied")


# ── Helpers ──────────────────────────────────────────────────────────


def _build_connector(connector_type: str, params: dict):
    if connector_type == "duckdb":
        return DuckDBConnector(
            db_path=params.get("db_path"),
            table_name=params.get("table_name"),
        )
    if connector_type == "postgres":
        return PostgresConnector(connection_params=params)
    if connector_type == "athena":
        from app.connectors.athena_connector import AthenaConnector
        return AthenaConnector(connection_params=params)
    raise HTTPException(status_code=400, detail=f"Unsupported connector type: {connector_type}")


def _build_connector_from_profile(profile: ConnectionProfile, secrets: SecretStore):
    params = secrets.get_by_connection_id(profile.connection_id)
    return _build_connector(profile.connector_type, params)


def _sanitize_table_name(raw: str) -> str:
    safe = _re.sub(r"[^A-Za-z0-9_]", "_", raw)
    safe = _re.sub(r"_+", "_", safe).strip("_")
    if not safe:
        safe = "uploaded_table"
    if safe[0].isdigit():
        safe = f"t_{safe}"
    return safe[:63]


def _quote_identifier(identifier: str) -> str:
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise HTTPException(status_code=400, detail="Invalid upload table name")
    return f'"{identifier}"'


async def _stream_upload_to_temp(
    *,
    upload: UploadFile,
    suffix: str,
    max_bytes: int,
) -> tuple[str, int]:
    bytes_written = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                tmp.close()
                os.unlink(tmp_path)
                raise HTTPException(status_code=413, detail=f"File exceeds {_MAX_UPLOAD_MB}MB limit")
            tmp.write(chunk)

    return tmp_path, bytes_written


def _cleanup_uploaded_artifact(
    *,
    profile: ConnectionProfile,
    params: dict,
    s3: S3Storage | None,
) -> None:
    if profile.connector_type != "duckdb":
        return

    db_path = params.get("db_path")
    if not isinstance(db_path, str) or not db_path:
        return

    if db_path.startswith("s3://"):
        if s3 is None:
            return
        try:
            s3.delete(s3.key_from_url(db_path))
        except Exception:
            logger.warning("Failed to delete uploaded S3 artifact for %s", profile.connection_id, exc_info=True)
        return

    try:
        local_path = Path(db_path)
        if local_path.exists():
            local_path.unlink()
    except Exception:
        logger.warning("Failed to delete uploaded local artifact for %s", profile.connection_id, exc_info=True)


def _cleanup_schema_artifacts(
    *,
    versions: list,
    schema_storage: BaseSchemaStorage | None,
) -> None:
    if schema_storage is None:
        return

    for version in versions:
        if not version.schema_path:
            continue
        try:
            schema_storage.delete(version.schema_path)
        except Exception:
            logger.warning("Failed to delete schema artifact %s", version.schema_path, exc_info=True)
