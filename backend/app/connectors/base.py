from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class SchemaContext:
    tables: dict[str, list[dict]]
    row_counts: dict[str, int]
    join_paths: list[dict]


class DataConnector(ABC):
    @abstractmethod
    def get_schema(self) -> SchemaContext:
        raise NotImplementedError

    @abstractmethod
    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_connector_type(self) -> str:
        raise NotImplementedError
