from __future__ import annotations

from app.connectors.base import DataConnector
from app.models import ChatResponse, SemanticIntent
from app.semantic import SemanticRegistry, build_sql_from_intent

from .intent_mapper import map_question_to_intent


class QueryService:
    def __init__(self, connector: DataConnector, registry: SemanticRegistry):
        self.connector = connector
        self.registry = registry

    def run_question(self, question: str, explicit_intent: SemanticIntent | None = None) -> ChatResponse:
        trace: list[str] = []
        intent = explicit_intent or map_question_to_intent(question)
        trace.append(f"Intent metric: {intent.metric}")
        trace.append(f"Intent dimensions: {', '.join(intent.dimensions) if intent.dimensions else 'none'}")

        sql = build_sql_from_intent(intent, self.registry)
        trace.append("SQL compiled from semantic registry")

        df = self.connector.execute_query(sql, limit=intent.limit)
        trace.append(f"Executed query and returned {len(df)} rows")

        return ChatResponse(
            question=question,
            intent=intent,
            sql=sql,
            rows=df.to_dict(orient="records"),
            row_count=len(df),
            trace=trace,
        )
