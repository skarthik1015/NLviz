from .base import DataConnector, SchemaContext


class PostgresConnector(DataConnector):
    def get_schema(self) -> SchemaContext:
        raise NotImplementedError("Postgres connector is planned for a later phase.")

    def execute_query(self, sql: str, limit: int = 5000):
        raise NotImplementedError("Postgres connector is planned for a later phase.")

    def test_connection(self) -> bool:
        raise NotImplementedError("Postgres connector is planned for a later phase.")

    def get_connector_type(self) -> str:
        return "postgres"
