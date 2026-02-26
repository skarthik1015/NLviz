from .base import DataConnector, SchemaContext
from .duckdb_connector import DuckDBConnector
from .postgres_connector import PostgresConnector
from .redshift_connector import RedshiftConnector

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
