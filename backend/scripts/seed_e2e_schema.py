"""Development and E2E only: publish the hand-written Olist schema on an existing connection to the
Olist Postgres database, so browser tests do not depend on LLM schema generation.

    docker compose exec backend python scripts/seed_e2e_schema.py "Olist (live Postgres)"

Stores the schema as a validated version (as a generation job would) and publishes it through the
running API, so activation and the value index happen exactly as for a generated schema. Prints the
connection id. Does nothing if the connection already has a published schema.
"""
from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppConfig  # noqa: E402
from app.models.connection import SemanticSchemaVersion, ValidationSummary  # noqa: E402
from app.semantic import load_semantic_registry  # noqa: E402
from app.services.connection_store import BaseConnectionStore  # noqa: E402
from app.storage.db_pool import DatabasePool  # noqa: E402
from app.storage.schema_storage import BaseSchemaStorage  # noqa: E402

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml"


def main(display_name: str, api: str = "http://127.0.0.1:8000") -> None:
    config = AppConfig.from_env()
    if config.environment == "production":
        raise SystemExit("refusing to seed a schema in production")
    pool = DatabasePool(config.database_url) if config.database_url else None
    store = BaseConnectionStore.create(config, pool=pool)
    matches = [c for c in store.list_connections() if c.display_name == display_name and c.status == "active"]
    if len(matches) != 1:
        raise SystemExit(f"expected one active connection named {display_name!r}, found {len(matches)}")
    connection = matches[0]
    published = store.get_published_version(connection.connection_id)
    if published is not None:
        storage = BaseSchemaStorage.create(config)
        try:
            storage.load(published.schema_path)
        except FileNotFoundError:  # storage lost (before schemas had a volume): restore the file
            storage.save(connection.connection_id, published.version_id, SCHEMA.read_text(encoding="utf-8"))
            print(f"restored the missing schema file for version {published.version_id}", file=sys.stderr)
        print(connection.connection_id)
        return

    registry = load_semantic_registry(SCHEMA)
    version_id = str(uuid.uuid4())
    path = BaseSchemaStorage.create(config).save(connection.connection_id, version_id, SCHEMA.read_text(encoding="utf-8"))
    metrics, dimensions = len(registry.schema.metrics), len(registry.schema.dimensions)
    store.create_version(SemanticSchemaVersion(
        version_id=version_id, connection_id=connection.connection_id, status="validated",
        created_at=datetime.now(timezone.utc), schema_path=path,
        validation_summary=ValidationSummary(total_metrics=metrics, valid_metrics=metrics,
                                             total_dimensions=dimensions, valid_dimensions=dimensions,
                                             confidence_score=1.0),
    ))
    request = urllib.request.Request(
        f"{api}/connections/{connection.connection_id}/publish", method="POST",
        data=json.dumps({"version_id": version_id}).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        assert json.loads(response.read())["status"] == "published"
    print(connection.connection_id)


if __name__ == "__main__":
    main(*sys.argv[1:])
