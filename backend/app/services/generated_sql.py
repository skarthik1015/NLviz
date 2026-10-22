"""SQL written by the model, run on the connection: the text-to-SQL baseline (/bench/sql) and the
app's guarded fallback (agent node sql_fallback, SQL_FALLBACK=on) share this.

The model gets the context NLviz's mapper gets (text_to_sql.build_context). Its SQL goes through
the safety gate in the connector. For the app's fallback it also goes through `pushdown_guard`
first: generated SQL is not limited to the shapes the compiler emits, so it can meet the engine's
known pushdown limitations of the engine:
- J31: DataFusion-only functions (approx_distinct) pushed to Postgres, which fails;
- J32: AVG over a NUMERIC column of a Postgres source fails; AVG arguments are cast to DOUBLE;
- J38: SELECT DISTINCT in a derived table fails on Postgres; GROUP BY is asked for instead.
DATE(ts) is written as a cast, which the engine has. Functions outside a known set are refused too. A refusal goes back to the model as a repair,
like an engine error.
"""
from __future__ import annotations

import json
from typing import Any

import sqlglot
from sqlglot import exp

from app.llm import build_completion_client, strip_json_fences

MAX_ROWS = 1000

# Functions the model may use: what the engine runs and pushes down correctly in our tests.
_ALLOWED_ANONYMOUS = {"date_trunc", "date_part", "to_char", "nullif", "coalesce", "round", "abs", "lower", "upper",
                      "length", "concat", "trim", "substr", "substring", "greatest", "least"}


class PushdownRefused(ValueError):
    pass


def pushdown_guard(sql: str) -> str:
    """The SQL with AVG arguments cast to DOUBLE, or PushdownRefused saying what to change."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise PushdownRefused(f"the SQL does not parse: {str(exc).splitlines()[0][:200]}") from None
    for func in tree.find_all(exp.Func):
        name = type(func).__name__
        if name.startswith("Approx"):
            raise PushdownRefused(f"{func.sql()[:60]} is not available on this engine; use the exact function "
                                  "(COUNT(DISTINCT ...))")
        if isinstance(func, exp.Anonymous) and func.name.lower() not in _ALLOWED_ANONYMOUS:
            raise PushdownRefused(f"function {func.name} is not available; use standard SQL functions")
    for select in tree.find_all(exp.Select):
        if select is not tree and select.args.get("distinct"):
            raise PushdownRefused("SELECT DISTINCT inside a subquery is not supported here; use GROUP BY instead")
    for day in list(tree.find_all(exp.Date)):
        # DATE(ts) is not a function on this engine ("Invalid function 'date'"); the cast is.
        if day.this is not None and not day.args.get("zone") and not day.expressions:
            day.replace(exp.Cast(this=day.this.copy(), to=exp.DataType.build("DATE")))
    for avg in list(tree.find_all(exp.Avg)):
        argument = avg.this
        if not isinstance(argument, exp.Cast):
            avg.set("this", exp.Cast(this=argument.copy(), to=exp.DataType.build("DOUBLE")))
    return tree.sql(dialect="postgres")


def generate_and_run(question: str, context: str, previous: tuple[str, str] | None, connector, to_physical,
                     intent_config, *, system_prompt: str, guard: bool, check=None,
                     instructions: list[str] | None = None) -> dict[str, Any]:
    """Ask the model for SQL and run it, repairing up to MAX_REPAIRS times. Returns {status:
    answered | unsupported | clarification | error, sql, rows, missing, error, steps, verdict,
    clarification}. `to_physical` maps the model's logical table names to the engine's.

    `check(sql, overridden) -> sql_checks.Verdict` (the SQL-primary path) runs on the guarded SQL
    before it executes: a repair goes back to the model like an engine error; a clarification ends
    the attempt for the user to answer; `instructions` are the user's answers to earlier ones."""
    from app.agent.state import MAX_REPAIRS  # lazy: app.agent imports this module

    task = intent_config.for_task("intent")
    client = build_completion_client(task.provider)
    prompt = [f"Database:\n{context}"]
    if previous:
        prompt.append(f"Previous question in this conversation: {previous[0]}\nIts SQL: {previous[1]}\n"
                      "If the new question follows up on it, start from that SQL.")
    prompt.append(f"Question: {question}")
    for instruction in instructions or ():
        prompt.append(f"The user decided: {instruction}")
    steps: list[dict] = []
    status, sql, rows, missing = "error", None, [], None
    verdict, refused_check = None, None
    for attempt in range(MAX_REPAIRS + 1):
        raw = client.complete_json(system_prompt=system_prompt, user_prompt="\n\n".join(prompt),
                                   model=task.model, timeout_ms=max(task.timeout_ms, 60000), max_tokens=1200)
        try:
            reply = json.loads(strip_json_fences(raw))
        except json.JSONDecodeError as exc:
            steps.append({"stage": "rejected", "attempt": attempt, "error": f"not JSON: {exc}"})
            prompt.append(f"Your answer was not valid JSON ({exc}). Answer again with JSON only.")
            continue
        if not reply.get("answerable", True):
            missing = str(reply.get("missing") or "not in this data")[:200]
            steps.append({"stage": "refused", "by": "llm", "reason": missing})
            status = "unsupported"
            break
        sql = reply.get("sql") or ""
        steps.append({"stage": "proposal", "attempt": attempt, "sql": sql})
        try:
            runnable = pushdown_guard(sql) if guard else sql
            if check is not None:
                overridden = {str(name) for name in reply.get("definitions_overridden") or ()}
                verdict = check(runnable, overridden)
                if verdict.clarification is not None:
                    steps.append({"stage": "clarification", "check": verdict.reason, "sql": runnable})
                    return {"status": "clarification", "sql": runnable, "rows": [], "missing": None, "error": None,
                            "steps": steps, "verdict": verdict, "clarification": verdict.clarification}
                if verdict.repair:
                    refused_check = verdict
                    steps.append({"stage": "rejected", "attempt": attempt, "check": verdict.reason,
                                  "error": verdict.repair})
                    prompt.append(f"Your SQL {sql!r} would give a wrong answer: {verdict.repair} "
                                  "Answer again with a corrected query.")
                    continue
                refused_check = None
            frame = connector.execute_query(to_physical(runnable), limit=MAX_ROWS)
        except PushdownRefused as exc:
            steps.append({"stage": "rejected", "attempt": attempt, "error": str(exc)})
            prompt.append(f"Your SQL {sql!r} cannot run here: {exc}. Answer again with a corrected query.")
            continue
        except Exception as exc:
            error = str(exc).splitlines()[0][:500] if str(exc) else type(exc).__name__
            steps.append({"stage": "execution_error", "attempt": attempt, "error": error})
            prompt.append(f"Your SQL {sql!r} failed: {error}. Answer again with a corrected query.")
            continue
        rows = json.loads(frame.to_json(orient="records", date_format="iso"))
        sql = runnable
        status = "answered"
        break
    if status == "error" and refused_check is not None:
        # The model kept writing SQL a check rejects (double counting, a definition left out): say
        # why, rather than answer with a number known to be wrong.
        status, missing = "unsupported", refused_check.repair
    error = None if status != "error" else (steps[-1].get("error") if steps else "no answer")
    return {"status": status, "sql": sql, "rows": rows[:MAX_ROWS], "missing": missing, "error": error, "steps": steps,
            "verdict": verdict, "clarification": None}
