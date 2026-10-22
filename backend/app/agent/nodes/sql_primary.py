"""The SQL-primary path (ANSWER_PATH=sql; docs/architecture/current-architecture.md): the model writes SQL
from the semantic layer's context, and the SQL is held to the layer before it runs.

    sql_answer ─┬─▶ END (answered, or unsupported with the reason)
        ▲       └─▶ sql_clarify (pause for the user) ─┘

1. Resolve meaning: value hints and examples for the question, the metric and dimension
   definitions, and the previous turn's SQL for a follow-up (text_to_sql.build_context).
2. Generate SQL: the model, told to reuse the definitions; it may say the data cannot answer.
3. Validate and execute: the pushdown guard (generated_sql), then the semantic protections
   (sql_checks: double counting, agreed definitions, mixed units, zeros), then the connector's
   safety gate. A failed check goes back to the model; a choice goes to the user, and the answer
   becomes an instruction for the next attempt.
4. Explain the actual query: the interpretation is read from the SQL that ran.

A question with an explicit intent (an edited interpretation) still goes through the compiler.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.charts import build_chart_spec
from app.models import SemanticIntent
from app.services.generated_sql import generate_and_run
from app.services.interpretation import interpretation_from_sql
from app.services.sql_checks import check_sql
from app.services.text_to_sql import SYSTEM_PROMPT, build_context, to_physical

PROMPT = SYSTEM_PROMPT.replace(
    "Answer with JSON only:",
    "A metric's definition includes its condition (WHERE ...): keep it. Only when the question explicitly asks "
    "for something else (\"including canceled orders\"), leave it out and name the metric in "
    "\"definitions_overridden\". Never add up a column of one table across a join to a table with several "
    "rows per row of it: aggregate that table in a subquery first, or filter with EXISTS / IN.\n"
    "Answer with JSON only:",
).replace('{"answerable": true, "sql": "SELECT ..."}',
          '{"answerable": true, "sql": "SELECT ...", "definitions_overridden": []}')


def _columns(schema) -> dict[str, set[str]]:
    return {table: {c.get("name") or c.get("column_name") for c in columns} for table, columns in schema.tables.items()}


def build_sql_answer_node(dependencies) -> Callable[[AgentState], AgentState]:
    registry, schema, mapper = dependencies.registry, dependencies.schema_context, dependencies.intent_mapper
    columns = _columns(schema)

    def physical(sql: str) -> str:
        return to_physical(sql, registry, dependencies.table_ref)

    def node(state: AgentState) -> AgentState:
        question = state.get("question", "")
        probed: dict[str, list[dict]] = {}

        def run(sql: str) -> list[dict]:
            # The same probe is asked once per question, however many repairs it takes.
            if sql not in probed:
                probed[sql] = dependencies.connector.execute_query(physical(sql), limit=5000).to_dict("records")
            return probed[sql]

        cache = state.get("grounding_cache") or {}
        if "hints" not in cache:
            found = mapper._examples(question)
            cache = {"hints": mapper._value_hints(question),
                     "examples": [{"question": e.question, "intent": e.intent} for e in found] if found else None}
        answered = state.get("sql_answered") or []
        # This turn's answers, then the definitions chosen earlier in the conversation.
        remembered = [{"reason": "definition", "constraint": c, "earlier": True}
                      for k, c in (state.get("policy_decisions") or {}).items()
                      if not any(a.get("reason") == "definition" and (a.get("constraint") or {}).get("key") == k
                                 for a in answered)]
        config = getattr(mapper, "config", None) or dependencies.intent_config
        ask_definitions = getattr(config, "ask_definitions", False)
        store = getattr(dependencies, "definitions", None)
        previous = state.get("previous_turn") or {}
        previous_sql = (previous["question"], previous["sql"]) if previous.get("sql") else None

        def check(sql: str, overridden: set[str]):
            # The user's choices are constraints the SQL is checked against, not checks switched off.
            return check_sql(sql, registry, columns, run=run, overridden=overridden, decisions=answered + remembered,
                             values=mapper._current_index(), ask_definitions=ask_definitions,
                             confirmed=store.all() if store is not None else None, question=question)

        result = generate_and_run(question, build_context(registry, schema, cache["hints"], cache["examples"]),
                                  previous_sql, dependencies.connector, physical, dependencies.intent_config,
                                  system_prompt=PROMPT, guard=True, check=check,
                                  instructions=[a["instruction"] for a in answered])
        audit = [{"stage": "sql_primary", "instructions": [a["instruction"] for a in answered]}, *result["steps"]]
        base = {"grounding_cache": cache, "audit": audit, "answer_path": "sql"}
        if result["status"] == "clarification":
            # The response needs an intent while it waits: an empty summary (the panel shows the question).
            return {**base, "pending_clarification": result["clarification"],
                    "intent": state.get("intent") or SemanticIntent(metric=""),
                    "user_trace": ["Checking the query: a choice is needed"],
                    "debug_trace": [f"SQL primary: asking ({result['clarification']['reason']})"]}
        if result["status"] != "answered":
            reason = result["missing"] or result["error"] or "no answer"
            return {**base, "pending_clarification": None, "sql": None, "rows": [], "row_count": 0,
                    "unsupported": reason, "intent": SemanticIntent(metric="", unsupported=reason),
                    "explanation": f"I can't answer this from the data: {reason}",
                    "user_trace": ["The question can't be answered from this data"],
                    "debug_trace": [f"SQL primary: {result['status']} ({reason[:200]})"]}
        rows, verdict = result["rows"], result["verdict"]
        intent = _intent(result["sql"], rows, verdict)
        return {
            **base,
            "pending_clarification": None,
            "unsupported": None,
            "sql_answered": [],
            "intent": intent,
            "intent_source": "llm",
            "sql": result["sql"],
            "rows": rows,
            "row_count": len(rows),
            "validation_status": "ok" if rows else "empty",
            "chart_spec": _chart(rows, intent, verdict.measures[0]["label"] if verdict and verdict.measures else None,
                                 _grouped(result["sql"])),
            "sql_verdict": _verdict(result["sql"], verdict, registry, columns),
            "explanation": _explain(rows, verdict),
            "previous_turn": {"question": question, "intent": {}, "sql": result["sql"]},
            "evidence": [{"kind": "note", "text": n} for n in (verdict.notes if verdict else [])],
            "user_trace": ["Query written and checked against the data model's definitions",
                           f"Query returned {len(rows)} row(s)"],
            "debug_trace": [f"SQL primary: answered, {len(rows)} row(s)"],
        }

    return node


def build_sql_clarify_node(dependencies=None) -> Callable[[AgentState], AgentState]:
    store = getattr(dependencies, "definitions", None)

    def node(state: AgentState) -> AgentState:
        pending = state["pending_clarification"]
        answer = interrupt(pending)
        # {"id": ..., "remember": true}: keep a definition for every question on this connection.
        remember = isinstance(answer, dict) and bool(answer.get("remember"))
        if isinstance(answer, dict):
            answer = answer.get("id")
        choices = pending["choices"]
        chosen = next((c for c in choices if c["id"] == str(answer)), None)
        if chosen is None:
            # Never invent a choice: an unknown or stale id asks again (route_after_sql_clarify).
            return {"pending_clarification": pending,
                    "user_trace": ["That answer is not one of the choices; please pick one"],
                    "debug_trace": [f"Clarify ({pending['reason']}): unknown answer {str(answer)[:40]!r}, asking again"]}
        update: dict = {}
        constraint = chosen.get("constraint") or {}
        trace = [f"Using your choice: {chosen['label']}"]
        if pending["reason"] == "definition":
            # Holds for this conversation; saved for the connection only when the user says so.
            update["policy_decisions"] = {**(state.get("policy_decisions") or {}), constraint["key"]: constraint}
            if remember and pending.get("rememberable") and store is not None:
                try:
                    store.confirm(constraint["key"], constraint.get("filter"), constraint.get("label", chosen["label"]),
                                  constraint.get("options") or [])
                    trace.append("Saved as this connection's definition for every question")
                except Exception:  # noqa: BLE001 - the answer still holds for this conversation
                    trace.append("Could not save the definition for the connection; it holds for this conversation")
        return {
            **update,
            "pending_clarification": None,
            "sql_answered": [*(state.get("sql_answered") or []),
                             {"reason": pending["reason"], "instruction": chosen["instruction"],
                              "constraint": chosen.get("constraint")}],
            "audit": [{"stage": "clarified", "reason": pending["reason"], "answer": answer}],
            "user_trace": trace,
            "debug_trace": [f"Clarify ({pending['reason']}): {chosen['label']}" + (" (remembered)" if remember else "")],
            # "You chose rio de janeiro for “Rio”"; "any" is no value; other choices name the decision.
            "evidence": [{"kind": "clarification", "dimension": pending.get("dimension") or "metric",
                          "text": pending.get("raw") or ("the definition" if pending["reason"] == "definition"
                                                          else pending["reason"].replace("_", " ")),
                          "value": None if (chosen.get("constraint") or {}).get("kind") == "any" else chosen["label"]}],
        }

    return node


def route_after_sql_answer(state: AgentState) -> str:
    return "clarify" if state.get("pending_clarification") else "done"


def route_after_sql_clarify(state: AgentState) -> str:
    return "ask_again" if state.get("pending_clarification") else "answer"


# ------------------------------------------------------------------------------- presentation
def _verdict(sql: str, verdict, registry, columns) -> dict:
    found = {"notes": verdict.notes if verdict else [], "measures": verdict.measures if verdict else [],
             "policies": verdict.policies if verdict else []}
    try:
        found["interpretation"] = interpretation_from_sql(sql, found, registry, columns).model_dump(mode="json")
    except Exception:  # noqa: BLE001 - the panel must never break an answer
        found["interpretation"] = None
    return found


_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([T ].*)?$")


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _grouped(sql: str) -> set[str]:
    """Output names of the query's GROUP BY columns (payment_type grouped is a label, not a measure)."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="postgres")
        group = tree.args.get("group") if isinstance(tree, exp.Select) else None
        if group is None:
            return set()
        outputs = tree.expressions
        names = set()
        for key in group.expressions:
            if isinstance(key, exp.Literal) and key.is_int and 0 < int(key.this) <= len(outputs):
                names.add(outputs[int(key.this) - 1].alias_or_name)
            elif isinstance(key, exp.Column):
                names |= {e.alias_or_name for e in outputs
                          if e.alias_or_name == key.name or (isinstance(e, exp.Alias) and e.this == key)}
        return names
    except Exception:  # noqa: BLE001 - a chart is a convenience
        return set()


def _shape(rows: list[dict], grouped: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """(label columns, number columns) of the result. Grouped columns are labels even when their
    values are numbers (payment type 1, 2, 3)."""
    if not rows:
        return [], []
    keys = list(rows[0])
    numbers = [k for k in keys if k not in grouped and all(_numeric(r.get(k)) or r.get(k) is None for r in rows)
               and any(_numeric(r.get(k)) for r in rows)]
    return [k for k in keys if k not in numbers], numbers


def _intent(sql: str, rows: list[dict], verdict) -> SemanticIntent:
    """A summary of the answer in the intent's shape, for the parts of the UI that show one. The
    interpretation, read from the SQL, is the real account of what was computed."""
    labels, numbers = _shape(rows)
    measure = (verdict.measures[0]["name"] if verdict and verdict.measures else numbers[-1] if numbers else "result")
    return SemanticIntent(metric=measure, dimensions=labels[:3], limit=min(max(len(rows), 1), 5000))


def _is_time(rows: list[dict], key: str) -> bool:
    values = [r.get(key) for r in rows if r.get(key) is not None]
    return bool(values) and all(isinstance(v, (date, datetime)) or (isinstance(v, str) and _DATE.match(v))
                                for v in values)


def _chart(rows: list[dict], intent: SemanticIntent, label: str | None = None,
           grouped: set[str] = frozenset()) -> dict | None:
    """One number per row, by up to two labels: a line over time, bars, or a single figure."""
    labels, numbers = _shape(rows, grouped)
    if not rows or len(numbers) != 1 or len(labels) > 2:
        return None
    value = numbers[0]
    # The measure's name ("Total Revenue"), not the SQL alias ("metric_value").
    title = label or value.replace("_", " ").title()
    time = next((k for k in labels if _is_time(rows, k)), None)
    others = [k for k in labels if k != time]
    if time:
        renamed = [{**{k: str(r.get(k)) for k in others}, "time_bucket": str(r.get(time)), "metric_value": r.get(value)}
                   for r in rows]
        stand_in = intent.model_copy(update={"dimensions": others, "time_dimension": time})
        return build_chart_spec("multi_line" if others else "line", renamed, stand_in, title)
    renamed = [{**{k: str(r.get(k)) for k in labels}, "metric_value": r.get(value)} for r in rows]
    stand_in = intent.model_copy(update={"dimensions": labels, "time_dimension": None})
    return build_chart_spec("bar" if labels else "stat", renamed, stand_in, title)


_SOURCES = {"confirmed": "confirmed for this connection", "conversation": "your choice in this conversation",
            "question": "as the question asks", "inferred": "the data model's unconfirmed guess"}


def _explain(rows: list[dict], verdict) -> str:
    parts = [f"{len(rows)} row(s)." if rows else "The query ran and returned no rows."]
    if verdict and verdict.measures:
        parts.append("Uses the data model's definition of " +
                     ", ".join(sorted({m["label"] for m in verdict.measures})) + ".")
    for policy in (verdict.policies if verdict else []):
        parts.append(f"Definition: {policy['statement']} ({_SOURCES.get(policy.get('source'), policy.get('source'))}).")
    parts += verdict.notes if verdict else []
    return " ".join(parts)
