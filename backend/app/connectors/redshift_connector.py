# TODO (future phase): Implement Redshift connector.
# Requires: redshift-connector or psycopg2 pointed at a Redshift endpoint.
# All methods currently raise NotImplementedError — do not expose via UI until implemented.
from .base import DataConnector, SchemaContext


class RedshiftConnector(DataConnector):
    def get_schema(self) -> SchemaContext:
        raise NotImplementedError("Redshift connector is planned for a later phase.")

    def execute_query(self, sql: str, limit: int = 5000):
        raise NotImplementedError("Redshift connector is planned for a later phase.")

    def test_connection(self) -> bool:
        raise NotImplementedError("Redshift connector is planned for a later phase.")

    def get_connector_type(self) -> str:
        return "redshift"
