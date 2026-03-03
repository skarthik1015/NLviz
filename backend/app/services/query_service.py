from __future__ import annotations

from uuid import uuid4

from app.agent import QueryGraphRunner
from app.models import ChatResponse, SemanticIntent


class QueryService:
    def __init__(self, query_graph: QueryGraphRunner):
        self.query_graph = query_graph

    def run_question(self, question: str, explicit_intent: SemanticIntent | None = None) -> ChatResponse:
        query_id = str(uuid4())
        state = self.query_graph.invoke(
            question=question,
            query_id=query_id,
            explicit_intent=explicit_intent,
        )
        intent = state.get("intent")
        sql = state.get("sql")
        if intent is None:
            raise RuntimeError("Query graph finished without an intent")
        if not sql:
            raise RuntimeError("Query graph finished without SQL")

        return ChatResponse(
            query_id=query_id,
            question=question,
            intent=intent,
            sql=sql,
            rows=state.get("rows", []),
            row_count=state.get("row_count", 0),
            trace=state.get("trace", []),
        )
