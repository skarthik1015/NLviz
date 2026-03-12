"""Async generation job manager using ThreadPoolExecutor."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from app.connectors.base import DataConnector
from app.models.connection import GenerationJob, SemanticSchemaVersion
from app.services.connection_store import ConnectionStore
from app.services.schema_generator import generate_semantic_schema
from app.services.intent_mapper import IntentMapperConfig

if TYPE_CHECKING:
    from app.storage.schema_storage import BaseSchemaStorage

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_JOBS = 3


class GenerationJobManager:
    def __init__(
        self,
        connection_store: ConnectionStore,
        schema_storage: "BaseSchemaStorage | None" = None,
        max_workers: int = _MAX_CONCURRENT_JOBS,
    ):
        self._store = connection_store
        self._schema_storage = schema_storage
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="schema-gen")

    def start_job(
        self,
        connection_id: str,
        connector: DataConnector,
        config: IntentMapperConfig | None = None,
    ) -> GenerationJob:
        job_id = str(uuid4())
        version_id = str(uuid4())
        now = datetime.now(timezone.utc)

        job = GenerationJob(
            job_id=job_id,
            connection_id=connection_id,
            status="queued",
            created_at=now,
        )
        with self._lock:
            self._jobs[job_id] = job

        self._executor.submit(
            self._run_job, job_id, connection_id, version_id, connector, config
        )
        return job

    def get_job(self, job_id: str) -> GenerationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run_job(
        self,
        job_id: str,
        connection_id: str,
        version_id: str,
        connector: DataConnector,
        config: IntentMapperConfig | None,
    ) -> None:
        self._update_status(job_id, "running")
        try:
            schema, yaml_path, validation, metadata = generate_semantic_schema(
                connector=connector,
                connection_id=connection_id,
                version_id=version_id,
                config=config,
                schema_storage=self._schema_storage,
            )

            # Confidence gating
            if validation.confidence_score < 0.3:
                self._update_status(
                    job_id,
                    "failed",
                    error=(
                        f"Schema confidence too low ({validation.confidence_score:.0%}). "
                        f"Only {validation.valid_metrics}/{validation.total_metrics} metrics passed validation. "
                        f"Broken: {', '.join(validation.broken_metrics)}. "
                        "Try a different LLM model or check table access permissions."
                    ),
                    validation_summary=validation,
                )
                return

            # Persist schema version record
            version = SemanticSchemaVersion(
                version_id=version_id,
                connection_id=connection_id,
                status="validated",
                created_at=datetime.now(timezone.utc),
                schema_path=str(yaml_path),
                validation_summary=validation,
                generation_metadata=metadata,
            )
            self._store.create_version(version)

            with self._lock:
                job = self._jobs[job_id]
                self._jobs[job_id] = job.model_copy(update={
                    "status": "succeeded",
                    "completed_at": datetime.now(timezone.utc),
                    "schema_version_id": version_id,
                    "validation_summary": validation,
                })

            logger.info(
                "Schema generation succeeded for connection %s: %d/%d metrics valid (%.0f%%)",
                connection_id,
                validation.valid_metrics,
                validation.total_metrics,
                validation.confidence_score * 100,
            )

        except Exception as exc:
            logger.exception("Schema generation failed for connection %s", connection_id)
            self._update_status(job_id, "failed", error=str(exc))

    def _update_status(
        self,
        job_id: str,
        status: str,
        error: str | None = None,
        validation_summary=None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            updates: dict = {"status": status}
            if status in ("succeeded", "failed"):
                updates["completed_at"] = datetime.now(timezone.utc)
            if error:
                updates["error"] = error
            if validation_summary:
                updates["validation_summary"] = validation_summary
            self._jobs[job_id] = job.model_copy(update=updates)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
