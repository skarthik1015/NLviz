"""Connection management endpoints: test, create, upload, generate, publish, delete."""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

import asyncio

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from app.connectors.mysql_connector import MySQLConnector
from app.connectors.postgres_connector import PostgresConnector
from app.dependencies import (
    get_audit_log,
    get_connection_service,
    get_connection_store,
    get_jammi_provisioner,
    get_jammi_upload_store,
    get_job_manager,
    get_mysql_settings,
    get_grounding,
    get_s3_import_allow_custom_endpoint,
    MySQLSettings,
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
    S3ImportRequestBody,
    AttachFileResponse,
)
from app.services.audit_log import AuditLog
from app.services.connection_service import ConnectionResolutionError, ConnectionService
from app.services.connection_store import BaseConnectionStore
from app.services.generation_job_manager import GenerationJobManager
from app.services.jammi_sources import (
    DATABASE_SOURCE_NAMES,
    JammiSourceProvisioner,
    SourceSpec,
    mysql_url,
    postgres_url,
)
from app.services.jammi_uploads import (
    UNSUPPORTED_FORMAT,
    UPLOAD_SUFFIXES,
    ManagedUploadStore,
    UploadError,
    convert_to_parquet,
    table_name_for,
)
from app.services.mysql_snapshot import snapshot_table_name, snapshot_tables
from app.services.postgres_snapshot import live_blockers, pre_epoch_timestamps, snapshot_postgres_tables
from app.services.s3_import import S3ImportError, S3ImportRequest, build_user_s3_client, download_object
from app.services.secret_store import BaseSecretStore
from app.storage.schema_storage import BaseSchemaStorage
from app.storage.s3_storage import S3Storage

router = APIRouter(prefix="/connections", tags=["connections"])

_MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "100"))
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB


# ── GET /connections ─────────────────────────────────────────────────


@router.get("")
async def list_connections(
    user: AuthUser = Depends(get_current_user),
    store: BaseConnectionStore = Depends(get_connection_store),
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
    audit.log("connection.test", owner_id=user.user_id, metadata={"connector_type": request.connector_type})
    # Discovery blocks on the database: off the event loop, like creation and uploads.
    return await asyncio.to_thread(_test_connection, request.connector_type, request.params)


def _test_connection(connector_type: str, params: dict) -> ConnectionTestResponse:
    """Errors come back as codes with plain words and the password redacted, like connection
    creation's; the raw driver text was returned before (review)."""
    try:
        connector = _build_connector(connector_type, params)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Connection test failed during connector init: %s", _redact(str(exc), params))
        return ConnectionTestResponse(success=False, error=_redact(_connection_error(str(exc), "CONNECTOR_BUILD_FAILED"), params))

    try:
        connection_ok = connector.test_connection()
    except Exception as exc:
        return ConnectionTestResponse(success=False, error=_redact(_connection_error(str(exc), "CONNECTION_TEST_FAILED"), params))
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
        logger.warning("Connection test failed during schema introspection: %s", _redact(str(exc), params))
        return ConnectionTestResponse(success=False, error="SCHEMA_INTROSPECTION_FAILED: the tables could not be read")
    finally:
        connector.close()


# ── POST /connections ────────────────────────────────────────────────


@router.post("", response_model=ConnectionCreateResponse)
async def create_connection(
    request: ConnectionCreateRequest,
    user: AuthUser = Depends(get_current_user),
    store: BaseConnectionStore = Depends(get_connection_store),
    secrets: BaseSecretStore = Depends(get_secret_store),
    audit: AuditLog = Depends(get_audit_log),
    provisioner: JammiSourceProvisioner | None = Depends(get_jammi_provisioner),
    jammi_uploads: ManagedUploadStore | None = Depends(get_jammi_upload_store),
    mysql_settings: MySQLSettings = Depends(get_mysql_settings),
) -> ConnectionCreateResponse:
    connection_id = str(uuid4())

    # Secrets Manager stays the source of truth for credentials, also for Jammi-backed connections.
    secrets.put(connection_id, request.params)

    engine = "legacy"
    registration = _Registration()
    register = None
    if request.connector_type == "mysql":
        if provisioner is None or (not mysql_settings.federation and jammi_uploads is None):
            secrets.delete(connection_id)
            raise HTTPException(status_code=400, detail="MYSQL_REQUIRES_JAMMI: MySQL connections are served by Jammi")
        if mysql_settings.federation:
            register = _register_mysql_with_jammi
        else:
            def register(provisioner, **kwargs):
                return _snapshot_mysql_into_jammi(
                    provisioner, jammi_uploads, max_rows=mysql_settings.snapshot_max_rows, **kwargs
                )
    elif request.connector_type == "postgres":
        if provisioner is None:
            secrets.delete(connection_id)
            raise HTTPException(status_code=400, detail="JAMMI_NOT_CONFIGURED: connections are served by Jammi")
        def register(provisioner, **kwargs):
            return _register_postgres_with_jammi(
                provisioner, jammi_uploads=jammi_uploads, max_rows=mysql_settings.snapshot_max_rows, **kwargs
            )
    if register is not None:
        try:
            # Discovery, snapshots and registration block on the network: keep them off the event loop.
            registration = await asyncio.to_thread(
                register, provisioner, workspace_id=user.user_id, connection_id=connection_id, params=request.params
            )
        except HTTPException:
            secrets.delete(connection_id)
            raise
        except UploadError as exc:
            secrets.delete(connection_id)
            raise HTTPException(status_code=400, detail=f"SNAPSHOT_FAILED: {_redact(str(exc), request.params)}") from None
        except Exception as exc:
            secrets.delete(connection_id)
            detail = _redact(str(exc), request.params)
            logger.warning("Jammi registration failed for connection %s: %s", connection_id, detail)
            raise HTTPException(status_code=400, detail=f"JAMMI_REGISTRATION_FAILED: {detail}") from None
        engine = "jammi"
    jammi_tables = registration.tables
    skipped_tables = registration.skipped_tables

    profile = ConnectionProfile(
        connection_id=connection_id,
        display_name=request.display_name,
        connector_type=request.connector_type,
        created_at=datetime.now(timezone.utc),
        owner_id=user.user_id,
        engine=engine,
        workspace_id=user.user_id if engine == "jammi" else None,
        jammi_tables=jammi_tables,
        mode="snapshot" if registration.mode == "snapshot" else "live",
        copied_at=datetime.now(timezone.utc) if registration.mode == "snapshot" else None,
        notes=list(registration.notes or []),
    )
    try:
        if registration.upload_urls or registration.declared_joins or registration.hidden_columns:
            # Delete reads the URLs to remove the snapshot files; schema generation reads the joins
            # and the hidden columns.
            secrets.put(connection_id, {**request.params, "upload_urls": registration.upload_urls,
                                        "declared_joins": registration.declared_joins,
                                        "hidden_columns": registration.hidden_columns})
        store.create_connection(profile)
    except Exception:
        # A connection and its Jammi sources (and snapshot files) are created as a unit.
        if engine == "jammi" and provisioner is not None:
            provisioner.deprovision(jammi_tables)
        _delete_managed_uploads(connection_id, registration.upload_urls, jammi_uploads)
        secrets.delete(connection_id)
        raise
    audit.log(
        "connection.create",
        owner_id=user.user_id,
        connection_id=connection_id,
        metadata={"engine": engine, "tables": len(jammi_tables), "skipped_tables": skipped_tables,
                  "mode": registration.mode},
    )

    return ConnectionCreateResponse(
        connection_id=connection_id,
        status="active",
        skipped_tables=skipped_tables,
        renamed_columns=registration.renamed_columns,
        mode="snapshot" if registration.mode == "snapshot" else "live",
        notes=registration.notes,
    )


# ── POST /connections/upload ─────────────────────────────────────────


@router.post("/upload", response_model=ConnectionCreateResponse)
async def upload_file(
    file: UploadFile = File(...),
    display_name: str = Form(...),
    user: AuthUser = Depends(get_current_user),
    store: BaseConnectionStore = Depends(get_connection_store),
    secrets: BaseSecretStore = Depends(get_secret_store),
    audit: AuditLog = Depends(get_audit_log),
    provisioner: JammiSourceProvisioner | None = Depends(get_jammi_provisioner),
    jammi_uploads: ManagedUploadStore | None = Depends(get_jammi_upload_store),
) -> ConnectionCreateResponse:
    if file.filename is None:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension before reading payload bytes.
    suffix = Path(file.filename).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_FORMAT)
    if provisioner is None or jammi_uploads is None:
        raise HTTPException(status_code=400, detail="JAMMI_NOT_CONFIGURED: uploads are served by Jammi")

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

        return await asyncio.to_thread(
            _create_jammi_upload_connection,
            tmp_path=Path(tmp_path),
            suffix=suffix,
            filename=file.filename,
            display_name=display_name,
            user=user,
            connection_id=connection_id,
            provisioner=provisioner,
            jammi_uploads=jammi_uploads,
            store=store,
            secrets=secrets,
            audit=audit,
        )
    finally:
        await file.close()
        if tmp_path and Path(tmp_path).exists():
            os.unlink(tmp_path)


# ── POST /connections/s3-import ──────────────────────────────────────


@router.post("/{connection_id}/files", response_model=AttachFileResponse)
async def attach_file(
    connection_id: str,
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    store: BaseConnectionStore = Depends(get_connection_store),
    secrets: BaseSecretStore = Depends(get_secret_store),
    audit: AuditLog = Depends(get_audit_log),
    conn_service: ConnectionService = Depends(get_connection_service),
    provisioner: JammiSourceProvisioner | None = Depends(get_jammi_provisioner),
    jammi_uploads: ManagedUploadStore | None = Depends(get_jammi_upload_store),
) -> AttachFileResponse:
    """Add a CSV/Parquet file to an existing connection as one more table. Jammi then joins it with
    the connection's other sources (e.g. a live Postgres database) in a single query."""
    if connection_id == "default":
        raise HTTPException(status_code=400, detail="Cannot change the demo connection")
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    _verify_owner(profile, user)
    if profile.status != "active":
        raise HTTPException(status_code=400, detail="Connection is archived")
    if profile.engine != "jammi":
        raise HTTPException(status_code=400, detail="CONNECTION_UNAVAILABLE: recreate this connection first")
    if provisioner is None or jammi_uploads is None:
        raise HTTPException(status_code=400, detail="JAMMI_NOT_CONFIGURED: files are served by Jammi")
    if file.filename is None:
        raise HTTPException(status_code=400, detail="No filename provided")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise HTTPException(status_code=400, detail=UNSUPPORTED_FORMAT)
    table = table_name_for(file.filename)
    if table in profile.jammi_tables or table in DATABASE_SOURCE_NAMES:
        raise HTTPException(status_code=409, detail=f"TABLE_EXISTS: the connection already has a table '{table}'")

    tmp_path: str | None = None
    try:
        tmp_path, bytes_written = await _stream_upload_to_temp(
            upload=file, suffix=suffix, max_bytes=_MAX_UPLOAD_MB * 1024 * 1024
        )
        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        url, provisioned, parquet = await asyncio.to_thread(
            _ingest_file, Path(tmp_path), suffix, table, user.user_id, connection_id, provisioner, jammi_uploads
        )
    finally:
        await file.close()
        if tmp_path and Path(tmp_path).exists():
            os.unlink(tmp_path)

    async with connection_lock(connection_id):
        # Re-read under the lock: another attach or a delete may have finished while this file was
        # ingested. Merging into the profile read before ingestion lost the other attach's table
        # (review B13).
        current = store.get_connection(connection_id)
        conflict = None
        if current is None or current.status != "active":
            conflict = (410, "Connection was deleted")
        elif table in current.jammi_tables:
            conflict = (409, f"TABLE_EXISTS: the connection already has a table '{table}'")
        if conflict is not None:
            provisioner.deprovision(provisioned.tables)
            _delete_managed_uploads(connection_id, [url], jammi_uploads)
            raise HTTPException(status_code=conflict[0], detail=conflict[1])
        params = secrets.get_by_connection_id(connection_id) or {}
        try:
            secrets.put(connection_id, {**params, "upload_urls": [*(params.get("upload_urls") or []), url]})
            store.update_jammi_tables(connection_id, {**current.jammi_tables, **provisioned.tables})
        except Exception:
            provisioner.deprovision(provisioned.tables)
            _delete_managed_uploads(connection_id, [url], jammi_uploads)
            secrets.put(connection_id, params)
            raise
        conn_service.evict_runtime(connection_id)
    audit.log("connection.attach_file", owner_id=user.user_id, connection_id=connection_id,
              metadata={"table": table, "rows": parquet.num_rows})
    return AttachFileResponse(connection_id=connection_id, table=table, renamed_columns=parquet.renamed_columns)


@router.post("/s3-import", response_model=ConnectionCreateResponse)
async def import_from_s3(
    body: S3ImportRequestBody,
    user: AuthUser = Depends(get_current_user),
    store: BaseConnectionStore = Depends(get_connection_store),
    secrets: BaseSecretStore = Depends(get_secret_store),
    audit: AuditLog = Depends(get_audit_log),
    provisioner: JammiSourceProvisioner | None = Depends(get_jammi_provisioner),
    jammi_uploads: ManagedUploadStore | None = Depends(get_jammi_upload_store),
    allow_custom_endpoint: bool = Depends(get_s3_import_allow_custom_endpoint),
) -> ConnectionCreateResponse:
    """Fetch one object with the user's credentials (never NLviz's, J25) and serve it like an upload."""
    if provisioner is None or jammi_uploads is None:
        raise HTTPException(status_code=400, detail="S3_IMPORT_REQUIRES_JAMMI: S3 import is served by Jammi")
    request = S3ImportRequest(
        s3_url=body.s3_url,
        region=body.region,
        access_key_id=body.access_key_id,
        secret_access_key=body.secret_access_key,
        session_token=body.session_token,
        endpoint_url=body.endpoint_url,
    )
    secret_values = {"password": body.secret_access_key or ""}
    connection_id = str(uuid4())
    with tempfile.TemporaryDirectory() as workdir:
        try:
            suffix, filename = request.suffix, request.filename
            local = Path(workdir) / f"object{suffix}"
            client = build_user_s3_client(request, allow_custom_endpoint=allow_custom_endpoint)
            await asyncio.to_thread(
                download_object, client, request, local, max_bytes=_MAX_UPLOAD_MB * 1024 * 1024
            )
        except S3ImportError as exc:
            raise HTTPException(status_code=400, detail=f"S3_IMPORT_FAILED: {_redact(str(exc), secret_values)}") from None
        response = await asyncio.to_thread(
            _create_jammi_upload_connection,
            tmp_path=local,
            suffix=suffix,
            filename=filename,
            display_name=body.display_name,
            user=user,
            connection_id=connection_id,
            provisioner=provisioner,
            jammi_uploads=jammi_uploads,
            store=store,
            secrets=secrets,
            audit=audit,
            source="s3",
        )
    return response


# ── POST /connections/{id}/generate ──────────────────────────────────


@router.post("/{connection_id}/generate", response_model=GenerateResponse)
async def generate_schema(
    connection_id: str,
    user: AuthUser = Depends(get_current_user),
    store: BaseConnectionStore = Depends(get_connection_store),
    secrets: BaseSecretStore = Depends(get_secret_store),
    job_mgr: GenerationJobManager = Depends(get_job_manager),
    audit: AuditLog = Depends(get_audit_log),
    conn_service: ConnectionService = Depends(get_connection_service),
) -> GenerateResponse:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    _verify_owner(profile, user)
    if profile.status != "active":
        raise HTTPException(status_code=400, detail="Connection is archived")

    try:
        connector = conn_service.build_connector(profile)
    except ConnectionResolutionError as exc:
        raise HTTPException(status_code=400, detail=f"CONNECTION_UNAVAILABLE: {exc.reason}") from None
    except Exception:
        logger.exception("Failed to build connector for generation job on connection %s", connection_id)
        raise HTTPException(status_code=500, detail="SCHEMA_GENERATION_FAILED")

    # Pre-flight: the key of the provider schema generation will actually use (LLM_PROVIDER_SCHEMA
    # when set), so the user gets a clear 503 now rather than a job failure later.
    from app.llm import missing_api_key
    from app.services.intent_mapper import IntentMapperConfig

    key_var = missing_api_key(IntentMapperConfig.from_env().for_task("schema").provider)
    if key_var:
        raise HTTPException(status_code=503, detail=f"LLM API key not configured: {key_var} is missing.")

    audit.log("schema.generate_start", owner_id=user.user_id, connection_id=connection_id)

    job = job_mgr.start_job(connection_id=connection_id, connector=connector)
    return GenerateResponse(job_id=job.job_id, status=job.status)


# ── GET /connections/{id}/jobs/{job_id} ──────────────────────────────


@router.get("/{connection_id}/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    connection_id: str,
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    job_mgr: GenerationJobManager = Depends(get_job_manager),
    store: BaseConnectionStore = Depends(get_connection_store),
) -> JobStatusResponse:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
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
    store: BaseConnectionStore = Depends(get_connection_store),
    audit: AuditLog = Depends(get_audit_log),
) -> PublishResponse:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
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

    audit.log("schema.publish", owner_id=user.user_id, connection_id=connection_id, metadata={"version_id": request.version_id})

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
    store: BaseConnectionStore = Depends(get_connection_store),
    secrets: BaseSecretStore = Depends(get_secret_store),
    conn_service: ConnectionService = Depends(get_connection_service),
    audit: AuditLog = Depends(get_audit_log),
    s3: S3Storage | None = Depends(get_upload_storage),
    schema_storage: BaseSchemaStorage | None = Depends(get_schema_storage),
    provisioner: JammiSourceProvisioner | None = Depends(get_jammi_provisioner),
    jammi_uploads: ManagedUploadStore | None = Depends(get_jammi_upload_store),
    grounding=Depends(get_grounding),
) -> dict:
    if connection_id == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default connection")

    async with connection_lock(connection_id):
        # Deprovisioning calls Jammi and storage: off the event loop.
        return await asyncio.to_thread(_delete_connection, connection_id, user, store, secrets, conn_service, audit,
                                       s3, schema_storage, provisioner, jammi_uploads, grounding)


def _delete_connection(connection_id, user, store, secrets, conn_service, audit, s3, schema_storage,
                       provisioner, jammi_uploads, grounding) -> dict:
    profile = store.get_connection(connection_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    _verify_owner(profile, user)

    params = secrets.get_by_connection_id(connection_id) or {}
    versions = store.list_versions(connection_id)

    store.archive_connection(connection_id)
    store.archive_versions_for_connection(connection_id)
    _cleanup_uploaded_artifact(profile=profile, params=params, s3=s3)
    _cleanup_schema_artifacts(versions=versions, schema_storage=schema_storage)
    failed_sources: list[str] = []
    if profile.engine == "jammi":
        if provisioner is not None:
            failed_sources = provisioner.deprovision(profile.jammi_tables)
        else:
            failed_sources = sorted({p.split(".", 1)[0] for p in profile.jammi_tables.values()})
            logger.warning(
                "Jammi is not configured; sources of %s were left registered: %s", connection_id, failed_sources
            )
        # After the sources are gone, so no source points at a missing file.
        _delete_managed_uploads(connection_id, params.get("upload_urls") or [], jammi_uploads)
        if grounding is not None and profile.workspace_id:
            try:
                grounding.remove(connection_id=connection_id, workspace_id=profile.workspace_id)
            except Exception:
                logger.warning("Could not remove the value index of %s", connection_id, exc_info=True)
                failed_sources.append("value index")
    secrets.delete(connection_id)
    conn_service.evict_runtime(connection_id)

    audit.log(
        "connection.delete",
        connection_id=connection_id,
        owner_id=user.user_id,
        metadata={"jammi_sources_not_removed": failed_sources} if failed_sources else None,
    )
    # The caller learns what was left behind, not only the log (review: deletion is best effort).
    result: dict = {"status": "archived", "connection_id": connection_id}
    if failed_sources:
        result["cleanup_incomplete"] = failed_sources
    return result


# ── Per-connection mutations ─────────────────────────────────────

_connection_locks: dict[str, asyncio.Lock] = {}


def connection_lock(connection_id: str) -> asyncio.Lock:
    """Serialises changes to one connection (attach, delete) within this process."""
    return _connection_locks.setdefault(connection_id, asyncio.Lock())


# ── Auth helpers ─────────────────────────────────────────────────


def _verify_owner(profile: ConnectionProfile, user: AuthUser) -> None:
    """Another user's connection is reported exactly like a missing one (404), as the question
    routes do: a 403 told a caller which connection ids exist (review: 403 here, 404 there)."""
    if profile.owner_id is not None and profile.owner_id != user.user_id:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")


# ── Helpers ──────────────────────────────────────────────────────────


@dataclass
class _Registration:
    tables: dict[str, str] = field(default_factory=dict)
    skipped_tables: list[str] = field(default_factory=list)
    upload_urls: list[str] = field(default_factory=list)  # snapshot files in managed storage
    renamed_columns: dict[str, str] = field(default_factory=dict)
    mode: str = "federated"
    declared_joins: list[dict] = field(default_factory=list)  # the source's foreign keys, logical names
    notes: list[str] = field(default_factory=list)  # for the user: why a database was copied, not queried live
    hidden_columns: list[str] = field(default_factory=list)  # "table.column" left out of the data model (J36)


def _register_postgres_with_jammi(
    provisioner: JammiSourceProvisioner,
    *,
    workspace_id: str,
    connection_id: str,
    params: dict,
    jammi_uploads: ManagedUploadStore | None = None,
    max_rows: int = 0,
) -> _Registration:
    """Discover the database's tables with NLviz's own driver connection (Jammi cannot list them
    remotely, issue J22) and register the database as one Jammi source."""
    schema = params.get("schema") or "public"
    if schema != "public":
        # Jammi Postgres sources expose only base tables of the public schema (issue J24).
        raise HTTPException(
            status_code=400,
            detail=(
                f"POSTGRES_SCHEMA_UNSUPPORTED: only tables in the 'public' schema can be queried "
                f"(got '{schema}'). Views are not supported either."
            ),
        )
    try:
        tables = PostgresConnector(connection_params=params).list_tables()
    except Exception as exc:
        # Discovery runs on NLviz's own driver connection, before Jammi is involved.
        raise HTTPException(
            status_code=400, detail=_connection_error(_redact(str(exc), params), "DATABASE_CONNECTION_FAILED")
        ) from None
    if not tables:
        raise HTTPException(status_code=400, detail="NO_TABLES: the 'public' schema has no tables")
    blockers = _postgres_live_blockers(params, tables)
    if blockers:
        # Jammi cannot register the database (J33) or would read money 100x too large (J34): copy it.
        if jammi_uploads is None:
            raise HTTPException(status_code=400, detail=(
                "POSTGRES_TYPES_UNSUPPORTED: these columns cannot be queried live and snapshot storage is not "
                f"configured: {', '.join(blockers[:5])}"))
        return _snapshot_postgres_into_jammi(provisioner, jammi_uploads, workspace_id=workspace_id,
                                             connection_id=connection_id, params=params, tables=tables,
                                             max_rows=max_rows, blockers=blockers)
    provisioned = provisioner.provision(
        workspace_id=workspace_id,
        connection_id=connection_id,
        specs=[SourceSpec(name="pg", url=postgres_url(params), tables=tables)],
    )
    # J36: pre-1970 `timestamp` values fail live queries that return them. Those columns are left out
    # of the data model rather than copying the whole database: everything else stays live.
    hidden = [c for c in _postgres_pre_epoch(params, tables) if c.split(".", 1)[0] in provisioned.tables]
    shown = ", ".join(hidden[:5]) + (f" and {len(hidden) - 5} more" if len(hidden) > 5 else "")
    notes = ([f"Your database is connected and stays live. {'One column was' if len(hidden) == 1 else 'Some columns were'} "
              f"left out: {shown}. {'It has' if len(hidden) == 1 else 'They have'} dates before 1970, which the query "
              f"engine can't read from a live database yet, so questions about {'it' if len(hidden) == 1 else 'them'} "
              "can't be answered. Everything else works as usual."] if hidden else [])
    return _Registration(tables=provisioned.tables, skipped_tables=provisioned.skipped_tables,
                         declared_joins=_postgres_foreign_keys(params, set(provisioned.tables)),
                         hidden_columns=hidden, notes=notes)


def _postgres_pre_epoch(params: dict, tables: list[str]) -> list[str]:
    """Columns with timestamps before 1970 (J36); [] when the check itself fails."""
    try:
        conn = PostgresConnector(connection_params=params)._connect()
        try:
            return pre_epoch_timestamps(conn, params.get("schema") or "public", tables)
        finally:
            conn.close()
    except Exception:
        logger.warning("Could not check timestamps; every column stays in", exc_info=True)
        return []


def _postgres_live_blockers(params: dict, tables: list[str]) -> list[str]:
    """Columns Jammi cannot read live; [] when the check itself fails (registration then reports)."""
    try:
        conn = PostgresConnector(connection_params=params)._connect()
        try:
            return live_blockers(conn, params.get("schema") or "public", tables)
        finally:
            conn.close()
    except Exception:
        logger.warning("Could not check column types; registering the database live", exc_info=True)
        return []


def _snapshot_postgres_into_jammi(provisioner: JammiSourceProvisioner, jammi_uploads: ManagedUploadStore, *,
                                  workspace_id: str, connection_id: str, params: dict, tables: list[str],
                                  max_rows: int, blockers: list[str]) -> _Registration:
    """Copy the tables into Parquet with NLviz's own driver and register one Jammi source per table,
    as for MySQL snapshots."""
    connector = PostgresConnector(connection_params=params)
    schema = params.get("schema") or "public"
    urls: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as workdir:
            snapshots = snapshot_postgres_tables(connector._connect, schema, tables, Path(workdir), max_rows=max_rows)
            for snap in snapshots:
                urls.append(jammi_uploads.put(snap.path, workspace_id=workspace_id, connection_id=connection_id,
                                              table=snap.table))
        provisioned = provisioner.provision(
            workspace_id=workspace_id, connection_id=connection_id,
            specs=[SourceSpec(name=snap.table, url=url, format="parquet", tables=[snap.table])
                   for snap, url in zip(snapshots, urls)],
        )
    except Exception:
        _delete_managed_uploads(connection_id, urls, jammi_uploads)
        raise
    renamed = {f"{s.table}.{old}": new for s in snapshots for old, new in s.renamed_columns.items()}
    joins = []
    for join in _postgres_foreign_keys(params, set(tables)):
        on = join["on"]
        for key, new in renamed.items():
            on = on.replace(key, f"{key.split('.')[0]}.{new}")
        joins.append({**join, "from": snapshot_table_name(join["from"]), "to": snapshot_table_name(join["to"]), "on": on})
    shown = ", ".join(blockers[:3]) + (f" and {len(blockers) - 3} more" if len(blockers) > 3 else "")
    return _Registration(
        tables=provisioned.tables, skipped_tables=provisioned.skipped_tables, upload_urls=urls,
        renamed_columns=renamed, mode="snapshot",
        declared_joins=[j for j in joins if j["from"] in provisioned.tables and j["to"] in provisioned.tables],
        notes=[f"This database was copied when you connected, so answers reflect that moment: the query engine "
               f"cannot read some of its columns live ({shown}). Reconnect to refresh the copy."],
    )


def _declared_join(from_table: str, from_column: str, to_table: str, to_column: str) -> dict:
    return {"from": from_table, "to": to_table, "type": "LEFT", "relationship": "many_to_one",
            "on": f"{from_table}.{from_column} = {to_table}.{to_column}"}


def _usable_joins(joins: list[dict], tables: set[str]) -> list[dict]:
    """Between registered tables, one per table pair (a second key between the same pair would
    make the join path ambiguous). Self-references are kept: the connector does not join them (the
    compiler has no table aliases) but marks their columns as keys (employee.reports_to)."""
    result, pairs = [], set()
    for join in joins:
        pair = frozenset((join["from"], join["to"]))
        if not pair <= tables or (pair in pairs and join["from"] != join["to"]):
            continue
        pairs.add(pair)
        result.append(join)
    return result


_PG_FOREIGN_KEYS = """
SELECT c.conname, src.relname, a.attname, dst.relname, fa.attname
FROM pg_constraint c
JOIN pg_class src ON src.oid = c.conrelid
JOIN pg_namespace n ON n.oid = src.relnamespace
JOIN pg_class dst ON dst.oid = c.confrelid
JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(attnum, fattnum, position) ON TRUE
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = k.fattnum
WHERE c.contype = 'f' AND n.nspname = %s
ORDER BY c.conname, k.position
"""


def _grouped_joins(rows) -> list[dict]:
    """One join per constraint: (constraint, from table, from column, to table, to column) rows, a
    composite key's columns ANDed into one condition rather than split into partial joins."""
    by_constraint: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for name, from_table, from_column, to_table, to_column in rows:
        by_constraint.setdefault((name, from_table, to_table), []).append((from_column, to_column))
    joins = []
    for (_, from_table, to_table), columns in by_constraint.items():
        join = _declared_join(from_table, columns[0][0], to_table, columns[0][1])
        join["on"] = " AND ".join(f"{from_table}.{fc} = {to_table}.{tc}" for fc, tc in columns)
        joins.append(join)
    return joins


def _postgres_foreign_keys(params: dict, tables: set[str]) -> list[dict]:
    """Declared foreign keys, read with NLviz's own driver (Jammi does not expose constraints).
    Schema generation joins on these first; without them only "<table>_id" names are matched.
    From pg_catalog: information_schema's constraint views list only tables the user owns, so a
    read-only user (the usual way to connect) saw none of Chinook's 11."""
    connector = PostgresConnector(connection_params=params)
    try:
        conn = connector._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_PG_FOREIGN_KEYS, (params.get("schema") or "public",))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("Could not read foreign keys; joins will be inferred from column names", exc_info=True)
        return []
    return _usable_joins(_grouped_joins(rows), tables)


def _mysql_foreign_keys(params: dict, tables: list[str], renamed: dict[str, str]) -> list[dict]:
    """Declared foreign keys, in the snapshot's names (snake_case tables; renamed columns)."""
    def column(table: str, name: str) -> str:
        return renamed.get(f"{snapshot_table_name(table)}.{name}") or name

    try:
        conn = MySQLConnector(connection_params=params)._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT constraint_name, table_name, column_name, referenced_table_name, referenced_column_name "
                    "FROM information_schema.key_column_usage "
                    "WHERE table_schema = %s AND referenced_table_name IS NOT NULL "
                    "ORDER BY constraint_name, ordinal_position",
                    (str(params["dbname"]),),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("Could not read foreign keys; joins will be inferred from column names", exc_info=True)
        return []
    wanted = set(tables)
    rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]
    renamed_rows = [(name, snapshot_table_name(t), column(t, c), snapshot_table_name(rt), column(rt, rc))
                    for name, t, c, rt, rc in rows if t in wanted and rt in wanted]
    return _usable_joins(_grouped_joins(renamed_rows), {snapshot_table_name(t) for t in tables})


def _ingest_file(
    tmp_path: Path,
    suffix: str,
    table: str,
    workspace_id: str,
    connection_id: str,
    provisioner: JammiSourceProvisioner,
    jammi_uploads: ManagedUploadStore,
):
    """Convert, store and register one file as a Jammi source; all or nothing."""
    with tempfile.TemporaryDirectory() as workdir:
        try:
            parquet = convert_to_parquet(tmp_path, suffix, Path(workdir) / f"{table}.parquet")
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=f"UPLOAD_INVALID: {exc}") from None
        url = jammi_uploads.put(parquet.path, workspace_id=workspace_id, connection_id=connection_id, table=table)
    try:
        provisioned = provisioner.provision(
            workspace_id=workspace_id,
            connection_id=connection_id,
            specs=[SourceSpec(name=table, url=url, format="parquet", tables=[table])],
        )
    except Exception as exc:
        _delete_managed_uploads(connection_id, [url], jammi_uploads)
        logger.warning("Jammi registration failed for a file of %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail="JAMMI_REGISTRATION_FAILED: the file could not be registered") from None
    return url, provisioned, parquet


def _create_jammi_upload_connection(
    *,
    tmp_path: Path,
    suffix: str,
    filename: str,
    display_name: str,
    user: AuthUser,
    connection_id: str,
    provisioner: JammiSourceProvisioner,
    jammi_uploads: ManagedUploadStore,
    store: BaseConnectionStore,
    secrets: BaseSecretStore,
    audit: AuditLog,
    source: str = "upload",
) -> ConnectionCreateResponse:
    """Convert the upload to Parquet, store it where jammi-server reads it, register it as one
    source. The file, the source and the connection are created as a unit."""
    table = table_name_for(filename)
    with tempfile.TemporaryDirectory() as workdir:
        try:
            parquet = convert_to_parquet(tmp_path, suffix, Path(workdir) / f"{table}.parquet")
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=f"UPLOAD_INVALID: {exc}") from None
        url = jammi_uploads.put(parquet.path, workspace_id=user.user_id, connection_id=connection_id, table=table)

    try:
        provisioned = provisioner.provision(
            workspace_id=user.user_id,
            connection_id=connection_id,
            specs=[SourceSpec(name=table, url=url, format="parquet", tables=[table])],
        )
    except Exception as exc:
        _delete_managed_uploads(connection_id, [url], jammi_uploads)
        logger.warning("Jammi registration failed for upload %s: %s", connection_id, exc)
        raise HTTPException(status_code=400, detail="JAMMI_REGISTRATION_FAILED: the file could not be registered") from None

    profile = ConnectionProfile(
        connection_id=connection_id,
        display_name=display_name,
        connector_type="upload",
        created_at=datetime.now(timezone.utc),
        owner_id=user.user_id,
        engine="jammi",
        workspace_id=user.user_id,
        jammi_tables=provisioned.tables,
        notes=list(parquet.notes or []),
    )
    try:
        # Not a secret, but the secret store is where connection params live (and delete reads them).
        secrets.put(connection_id, {"upload_urls": [url]})
        store.create_connection(profile)
    except Exception:
        provisioner.deprovision(provisioned.tables)
        _delete_managed_uploads(connection_id, [url], jammi_uploads)
        secrets.delete(connection_id)
        raise
    audit.log(
        "connection.create",
        owner_id=user.user_id,
        connection_id=connection_id,
        metadata={"source": source, "engine": "jammi", "rows": parquet.num_rows,
                  "renamed_columns": parquet.renamed_columns},
    )
    return ConnectionCreateResponse(
        connection_id=connection_id, status="active", renamed_columns=parquet.renamed_columns, notes=parquet.notes
    )


def _delete_managed_uploads(connection_id: str, urls: list[str], jammi_uploads: ManagedUploadStore | None) -> None:
    for url in urls:
        if jammi_uploads is None:
            logger.warning("Upload storage is not configured; %s of %s was left in place", url, connection_id)
            continue
        try:
            jammi_uploads.delete(url)
        except Exception:
            logger.warning("Failed to delete uploaded file %s of %s", url, connection_id, exc_info=True)


def _register_mysql_with_jammi(
    provisioner: JammiSourceProvisioner,
    *,
    workspace_id: str,
    connection_id: str,
    params: dict,
) -> _Registration:
    """Live federation (MYSQL_FEDERATION=true): register the database as one Jammi source, which
    Jammi exposes under the schema `public`. Off by default because of issue J27."""
    tables = _discover_mysql_tables(params)
    provisioned = provisioner.provision(
        workspace_id=workspace_id,
        connection_id=connection_id,
        specs=[SourceSpec(name="mysql", url=mysql_url(params), tables=tables)],
    )
    return _Registration(tables=provisioned.tables, skipped_tables=provisioned.skipped_tables)


def _snapshot_mysql_into_jammi(
    provisioner: JammiSourceProvisioner,
    jammi_uploads: ManagedUploadStore,
    *,
    workspace_id: str,
    connection_id: str,
    params: dict,
    max_rows: int,
) -> _Registration:
    """Default: copy the tables into Parquet with NLviz's own driver, store them like uploads, and
    register one Jammi source per table. Jammi then evaluates queries with DataFusion semantics."""
    discovered = _discover_mysql_tables(params)
    # The snapshot is NLviz's own copy, so mixed-case tables are stored under snake_case names
    # (InvoiceLine -> invoice_line); only a second table with the same snake_case name is skipped.
    tables, skipped, logical_names = [], [], set()
    for table in discovered:
        logical = snapshot_table_name(table)
        if logical in logical_names:
            skipped.append(table)
            continue
        logical_names.add(logical)
        tables.append(table)
    connector = MySQLConnector(connection_params=params)
    urls: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as workdir:
            snapshots = snapshot_tables(
                connector._connect, str(params["dbname"]), tables, Path(workdir), max_rows=max_rows
            )
            for snap in snapshots:
                urls.append(jammi_uploads.put(
                    snap.path, workspace_id=workspace_id, connection_id=connection_id, table=snap.table
                ))
        provisioned = provisioner.provision(
            workspace_id=workspace_id,
            connection_id=connection_id,
            specs=[
                SourceSpec(name=snap.table, url=url, format="parquet", tables=[snap.table])
                for snap, url in zip(snapshots, urls)
            ],
        )
    except Exception:
        _delete_managed_uploads(connection_id, urls, jammi_uploads)
        raise
    renamed = {f"{s.table}.{old}": new for s in snapshots for old, new in s.renamed_columns.items()}
    declared = _mysql_foreign_keys(params, tables, renamed)
    renamed.update({source: snapshot_table_name(source) for source in tables if snapshot_table_name(source) != source})
    return _Registration(
        tables=provisioned.tables,
        skipped_tables=skipped + provisioned.skipped_tables,
        upload_urls=urls,
        renamed_columns=renamed,
        mode="snapshot",
        declared_joins=[j for j in declared if j["from"] in provisioned.tables and j["to"] in provisioned.tables],
        # Ops check (2026-10-11): an invoice added in MySQL never appeared, and nothing said why.
        notes=["This database was copied when you connected, so answers reflect that moment. "
               "Reconnect to refresh the copy."],
    )


def _discover_mysql_tables(params: dict) -> list[str]:
    """Tables and views, listed with NLviz's own driver connection (Jammi cannot list them, J22)."""
    try:
        tables = MySQLConnector(connection_params=params).list_tables()
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=_connection_error(_redact(str(exc), params), "DATABASE_CONNECTION_FAILED")
        ) from None
    if not tables:
        raise HTTPException(status_code=400, detail="NO_TABLES: the database has no tables")
    return tables


def _connection_error(message: str, fallback: str) -> str:
    """A driver error as a code and plain words. Creating a connection returned the raw driver text
    ("connection to server at "host.docker.internal" (192.168.65.254), port 1 failed...") while the
    test button already said "cannot reach host"; both use this now."""
    lowered = message.lower()
    if "password authentication" in lowered or "authentication failed" in lowered or "access denied" in lowered:
        return "AUTH_FAILED: wrong username or password"
    if "timeout" in lowered or "timed out" in lowered:
        return "TIMEOUT: connection timed out — check host and port"
    if ("could not connect" in lowered or "connection refused" in lowered or "can't connect to mysql" in lowered
            or "could not translate host name" in lowered or "name or service not known" in lowered):
        return "NETWORK_UNREACHABLE: cannot reach host — check host and port"
    if "does not exist" in lowered or "unknown database" in lowered:
        return f"DATABASE_NOT_FOUND: {message.strip()[:200]}"
    return f"{fallback}: {message.strip()[:300]}"


def _redact(message: str, params: dict) -> str:
    """Jammi errors can echo a source URL; never return or log the password (issue J6)."""
    from urllib.parse import quote

    password = str(params.get("password") or "")
    for secret in {password, quote(password, safe="")} - {""}:
        message = message.replace(secret, "***")
    return message[:500]


def _build_connector(connector_type: str, params: dict):
    """Discovery connectors for the connection test (queries themselves go through Jammi)."""
    if connector_type == "postgres":
        return PostgresConnector(connection_params=params)
    if connector_type == "mysql":
        return MySQLConnector(connection_params=params)
    raise HTTPException(status_code=400, detail=f"Unsupported connector type: {connector_type}")


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
