from .base import DataConnector, SchemaContext
from .jammi_connector import JammiConnector
from .mysql_connector import MySQLConnector
from .postgres_connector import PostgresConnector

# DuckDB is the reference engine for the golden tests and the offline fallback for the bundled
# demo when Jammi is not configured (development only). User connections are served by Jammi.
try:
    from .duckdb_connector import DuckDBConnector
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    DuckDBConnector = None

__all__ = [
    "DataConnector",
    "DuckDBConnector",
    "JammiConnector",
    "MySQLConnector",
    "PostgresConnector",
    "SchemaContext",
]
