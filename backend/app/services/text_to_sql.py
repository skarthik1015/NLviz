"""Approach A of the ablation (docs/ablation-results.md): the LLM writes SQL directly.

For a fair comparison it gets the same business context NLviz's intent mapper gets on the same
connection: the tables and columns, the joins, every metric and dimension with its SQL definition,
the value hints for the question, and the retrieved examples (compiled to SQL by NLviz's compiler).
Its SQL passes the same safety gate and runs on the same engine. It may say the data cannot answer;
an engine or safety error goes back to it with the message, at most MAX_REPAIRS times (as NLviz).

Only for measuring: served by /bench/sql when BENCH_ENDPOINTS=on, never in production.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import sqlglot
from sqlglot import exp

from app.llm import usage_meter
from app.semantic import build_sql_from_intent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You answer business questions about a database by writing one SQL query.
The engine is Apache DataFusion (PostgreSQL-like SQL): date_trunc('month', col), EXTRACT(year FROM col),
CAST(x AS DOUBLE), double quotes for identifiers, no backticks. Use only the tables and columns listed.
Use the metric and dimension definitions for business terms ("revenue", "customers", ...): they are the
agreed meaning. Value hints are real values from the data that the question may refer to.
If the data cannot answer the question (the information is not there, or it asks for a prediction or
an opinion), do not guess: say so.
Answer with JSON only: {"answerable": true, "sql": "SELECT ..."} or {"answerable": false, "missing": "what is missing"}."""

# Conversation memory for follow-ups: conversation id -> (question, sql). Bounded; bench only.
_HISTORY: dict[str, tuple[str, str]] = {}
_HISTORY_LOCK = threading.Lock()
_HISTORY_MAX = 2000


def _metric_sql(metric) -> str:
    if metric.aggregation == "RATIO":
        definition = f"({metric.numerator_sql}) / NULLIF(({metric.denominator_sql}), 0)"
    elif metric.aggregation == "COUNT_DISTINCT":
        definition = f"COUNT(DISTINCT {metric.sql_expression})"
    else:
        definition = f"{metric.aggregation}({metric.sql_expression})"
    return definition + (f" WHERE {metric.base_filter}" if metric.base_filter else "")


def build_context(registry, schema, value_hints: list[dict] | None, examples: list[dict] | None) -> str:
    # Each column with its type. Read as "column_name" until 2026-10-17, a key the schema does not
    # use: every column was sent as null, and the ablation's baseline saw columns only through the
    # metric and dimension definitions (found in review).
    tables = {name: [f"{c.get('name') or c.get('column_name')} {c.get('type') or ''}".strip() for c in columns]
              for name, columns in schema.tables.items()}
    sql_examples = []
    for example in examples or ():
        try:
            from app.models import SemanticIntent

            sql = build_sql_from_intent(SemanticIntent.model_validate(example["intent"]), registry)
        except Exception:  # an example the compiler rejects is left out, as NLviz would
            continue
        sql_examples.append({"question": example["question"], "sql": sql})
    context: dict[str, Any] = {
        "tables": tables,
        "joins": [{"from": j.from_table, "to": j.to_table, "on": j.on,
                   **({"relationship": j.relationship} if j.relationship else {})} for j in registry.schema.joins],
        "metrics": [{"name": m.name, "description": m.description, "sql": _metric_sql(m),
                     "tables": m.required_tables} for m in registry.schema.metrics],
        "dimensions": [{"name": d.name, "display_name": d.display_name, "sql": d.sql_expression,
                        "tables": d.required_tables} for d in registry.schema.dimensions],
        "time_columns": [{"name": t.name, "sql": t.sql_expression, "table": t.table} for t in registry.schema.time_dimensions],
    }
    if value_hints:
        context["value_hints"] = value_hints
    if sql_examples:
        context["examples"] = sql_examples
    return json.dumps(context, separators=(",", ":"), default=str)


def to_physical(sql: str, registry, table_ref) -> str:
    """Logical table names (as in the context) to the engine's qualified names, keeping the logical
    name as the alias so `orders.col` still resolves."""
    if table_ref is None:
        return sql
    logical = {t.name for t in registry.schema.tables} | set()
    tree = sqlglot.parse_one(sql, read="postgres")
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in list(tree.find_all(exp.Table)):
        name = table.name
        if name in ctes or table.args.get("db") or name not in logical:
            continue
        parts = table_ref(name).split(".")
        replacement = exp.table_(parts[-1], db=parts[-2] if len(parts) > 1 else None,
                                 catalog=parts[-3] if len(parts) > 2 else None,
                                 alias=table.alias or name)
        table.replace(replacement)
    return tree.sql(dialect="postgres")


def answer(question: str, conversation_id: str | None, runtime, intent_config, guard: bool = False) -> dict[str, Any]:
    from app.services.generated_sql import generate_and_run

    dependencies = runtime.query_graph.dependencies
    registry, schema, mapper = dependencies.registry, dependencies.schema_context, dependencies.intent_mapper
    started = time.perf_counter()
    with usage_meter() as calls:
        hints = mapper._value_hints(question)
        found = mapper._examples(question)
        examples = [{"question": e.question, "intent": e.intent} for e in found] if found else None
        with _HISTORY_LOCK:
            previous = _HISTORY.get(conversation_id or "")
        # Unguarded by default: the baseline as measured in the ablation; guard=True is the app's SQL path.
        result = generate_and_run(question, build_context(registry, schema, hints, examples), previous,
                                  runtime.connector, lambda sql: to_physical(sql, registry, dependencies.table_ref),
                                  intent_config, system_prompt=SYSTEM_PROMPT, guard=guard)
    status, sql, rows = result["status"], result["sql"], result["rows"]
    if status == "answered" and conversation_id:
        with _HISTORY_LOCK:
            if len(_HISTORY) >= _HISTORY_MAX:
                _HISTORY.pop(next(iter(_HISTORY)))
            _HISTORY[conversation_id] = (question, sql)
    run = {**(getattr(runtime.query_graph, "run_info", None) or {}),
           "approach": "text_to_sql_guarded" if guard else "text_to_sql"}
    run.pop("phrase_rewrites", None)
    return {
        "question": question, "status": status, "sql": sql, "rows": rows, "row_count": len(rows),
        "unsupported": result["missing"], "conversation_id": conversation_id, "clarification": None,
        "error": result["error"], "seconds": round(time.perf_counter() - started, 2),
        "audit": {"run": run, "steps": result["steps"], "llm_calls": calls,
                  "tokens": {"prompt": sum(c.get("prompt_tokens") or 0 for c in calls),
                             "completion": sum(c.get("completion_tokens") or 0 for c in calls)}},
    }
