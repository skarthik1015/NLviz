"""A published schema whose file is gone (storage lost in a redeploy) must give a clear error, not a 500."""
from __future__ import annotations

import pytest

from app.connectors import DuckDBConnector
from app.services.connection_service import ConnectionResolutionError, ConnectionService
from app.services.intent_mapper import IntentMapperConfig


class _GoneStorage:
    def load(self, path):
        raise FileNotFoundError(path)


def test_missing_schema_file_is_a_resolution_error():
    service = ConnectionService(connection_store=None, secret_store=None,
                                intent_config=IntentMapperConfig(mode="heuristic"), schema_storage=_GoneStorage())
    with pytest.raises(ConnectionResolutionError, match="schema file is missing"):
        service._build_runtime(DuckDBConnector(), "/app/data/schemas/c/v.yaml")
