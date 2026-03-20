from .base import DataConnector, SchemaContext
from .postgres_connector import PostgresConnector
from .redshift_connector import RedshiftConnector

try:
    from .duckdb_connector import DuckDBConnector
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    DuckDBConnector = None

try:
    from .athena_connector import AthenaConnector
except ModuleNotFoundError:  # pragma: no cover - pyathena not installed
    AthenaConnector = None

CONNECTOR_REGISTRY = {
    "duckdb": DuckDBConnector,
    "postgres": PostgresConnector,
    "redshift": RedshiftConnector,
    "athena": AthenaConnector,
}

__all__ = [
    "CONNECTOR_REGISTRY",
    "AthenaConnector",
    "DataConnector",
    "DuckDBConnector",
    "PostgresConnector",
    "RedshiftConnector",
    "SchemaContext",
]
