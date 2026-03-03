from .base import DataConnector, SchemaContext
from .postgres_connector import PostgresConnector
from .redshift_connector import RedshiftConnector

try:
    from .duckdb_connector import DuckDBConnector
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    DuckDBConnector = None

CONNECTOR_REGISTRY = {
    "duckdb": DuckDBConnector,
    "postgres": PostgresConnector,
    "redshift": RedshiftConnector,
    "snowflake": None,
    "athena": None,
}

__all__ = [
    "CONNECTOR_REGISTRY",
    "DataConnector",
    "DuckDBConnector",
    "PostgresConnector",
    "RedshiftConnector",
    "SchemaContext",
]
