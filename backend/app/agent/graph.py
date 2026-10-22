"""The query agent (design doc §5.2, Phase 3).

    map_intent ─▶ check_intent ─┬─▶ sql_builder ─▶ executor ─┬─▶ validator ─┬─▶ chart_selector ─▶ explainer
        ▲   ▲          ▲        │                            │              │
        │   │          └ clarify┤ (pause for the user)       │              └─▶ sql_builder (retry hints)
        │   └───── repair ──────┘                            │
        └──────────── repair (engine error) ─────────────────┘

State is kept per conversation thread by a checkpointer, so a follow-up question sees the previous
turn and a clarification can pause the graph until the user answers.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.nodes import (
    build_chart_selector_node,
    build_check_intent_node,
    build_clarify_node,
    build_executor_node,
    build_explainer_node,
    build_unsupported_node,
    build_map_intent_node,
    build_sql_builder_node,
    build_validator_node,
    route_after_check,
    route_after_execute,
    route_after_validator,
)
from app.agent.nodes.sql_primary import (build_sql_answer_node, build_sql_clarify_node, route_after_sql_answer,
                                        route_after_sql_clarify)
from app.agent.nodes.sql_fallback import FALLBACK_REASONS, build_sql_fallback_node, route_after_fallback
from app.agent.state import AgentState, build_initial_state
from app.connectors.base import DataConnector, SchemaContext
from app.models import SemanticIntent
from app.semantic import SemanticRegistry, build_sql_from_intent
from app.semantic.sql_builder import TableRefResolver
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter

# Threads for one-off questions (no conversation id); deleted once answered.
_SINGLE_PREFIX = "single:"


class NoPendingClarificationError(ValueError):
    pass


@dataclass(frozen=True)
class QueryGraphDependencies:
    connector: DataConnector
    schema_context: SchemaContext
    registry: SemanticRegistry
    intent_mapper: IntentMapperRouter
    intent_config: IntentMapperConfig
    # Logical -> physical table names for engines that need qualified references (Jammi).
    table_ref: TableRefResolver | None = None
    # Definitions the owner confirmed for this connection (app.services.definitions); None: none kept.
    definitions: object | None = None


def _timed(name: str, node: Callable[[AgentState], AgentState]) -> Callable[[AgentState], AgentState]:
    """Adds the node's duration to the debug trace (and to `timings_ms`, for the trace panel)."""

    def run(state: AgentState) -> AgentState:
        started = time.perf_counter()
        update = node(state)  # a clarification interrupt propagates unchanged
        elapsed = round((time.perf_counter() - started) * 1000)
        return {**update, "debug_trace": [*update.get("debug_trace", []), f"Timing: {name} {elapsed} ms"],
                "timings_ms": [{"node": name, "ms": elapsed}]}

    return run


def build_query_graph(dependencies: QueryGraphDependencies, checkpointer=None):
    graph = StateGraph(AgentState)

    def add(name: str, node) -> None:
        graph.add_node(name, _timed(name, node))  # type: ignore[arg-type]
    mapper, registry, schema = dependencies.intent_mapper, dependencies.registry, dependencies.schema_context

    add("map_intent", build_map_intent_node(mapper, registry, schema))
    def probe(intent) -> list[dict]:
        """Compile and run an intent; the ambiguity checks look at its rows (e.g. units per group)."""
        sql = build_sql_from_intent(intent, registry, table_ref=dependencies.table_ref)
        return dependencies.connector.execute_query(sql, limit=intent.limit).to_dict("records")

    add("check_intent", build_check_intent_node(mapper, registry, schema, probe))
    add("clarify", build_clarify_node())
    add("sql_builder", build_sql_builder_node(registry, dependencies.table_ref))
    add("executor", build_executor_node(dependencies.connector))
    add("validator", build_validator_node())
    add("chart_selector", build_chart_selector_node())
    add("explainer", build_explainer_node(dependencies.intent_config, dependencies.registry))
    add("unsupported", build_unsupported_node(dependencies.registry))
    config = getattr(dependencies.intent_mapper, "config", None) or dependencies.intent_config
    fallback_on = getattr(config, "sql_fallback", False)
    if fallback_on:
        add("sql_fallback", build_sql_fallback_node(dependencies))

    # SQL-primary needs a model to write SQL; heuristic mode keeps the compiler.
    sql_primary = getattr(config, "answer_path", "semantic") == "sql" and config.mode == "llm"
    if sql_primary:
        add("sql_answer", build_sql_answer_node(dependencies))
        add("sql_clarify", build_sql_clarify_node(dependencies))
        # An explicit intent (an edited interpretation) still goes through the compiler.
        graph.add_conditional_edges(START, lambda state: "semantic" if state.get("explicit_intent") else "sql",
                                    {"semantic": "map_intent", "sql": "sql_answer"})
        graph.add_conditional_edges("sql_answer", route_after_sql_answer,  # type: ignore[arg-type]
                                    {"clarify": "sql_clarify", "done": END})
        graph.add_conditional_edges("sql_clarify", route_after_sql_clarify,  # type: ignore[arg-type]
                                    {"ask_again": "sql_clarify", "answer": "sql_answer"})
    else:
        graph.add_edge(START, "map_intent")
    graph.add_edge("map_intent", "check_intent")
    def route_check(state: AgentState) -> str:
        route = route_after_check(state)
        if route == "unsupported" and fallback_on and state.get("refused_by") in FALLBACK_REASONS:
            return "sql_fallback"
        return route

    graph.add_conditional_edges(
        "check_intent",
        route_check,  # type: ignore[arg-type]
        {"repair": "map_intent", "clarify": "clarify", "compile": "sql_builder", "unsupported": "unsupported",
         **({"sql_fallback": "sql_fallback"} if fallback_on else {})},
    )
    if fallback_on:
        graph.add_conditional_edges("sql_fallback", route_after_fallback,  # type: ignore[arg-type]
                                    {"unsupported": "unsupported", "done": END})
    graph.add_edge("unsupported", END)
    graph.add_edge("clarify", "check_intent")
    graph.add_edge("sql_builder", "executor")
    graph.add_conditional_edges(
        "executor",
        route_after_execute,  # type: ignore[arg-type]
        {"repair": "map_intent", "validate": "validator"},
    )
    # Validator: retry back to sql_builder or continue to chart_selector
    graph.add_conditional_edges(
        "validator",
        route_after_validator,  # type: ignore[arg-type]
        {"retry": "sql_builder", "continue": "chart_selector"},
    )
    graph.add_edge("chart_selector", "explainer")
    graph.add_edge("explainer", END)

    return graph.compile(checkpointer=checkpointer)


def _run_info(dependencies: QueryGraphDependencies) -> dict[str, Any]:
    """What produced an answer, for the audit trail: the model, whether the phrase rules ran, and
    which semantic layer (a fingerprint changes whenever anything in it changes)."""
    config = getattr(dependencies.intent_mapper, "config", None) or dependencies.intent_config
    intent = config.for_task("intent") if config.mode == "llm" else config
    schema = dependencies.registry.schema
    return {
        "mode": config.mode,
        "provider": intent.provider if config.mode == "llm" else None,
        "model": intent.model if config.mode == "llm" else None,
        "phrase_rewrites": getattr(config, "phrase_rewrites", True),
        "sql_fallback": getattr(config, "sql_fallback", False),
        "answer_path": getattr(config, "answer_path", "semantic") if config.mode == "llm" else "semantic",
        "ask_definitions": getattr(config, "ask_definitions", False),
        "schema": {"dataset": schema.dataset, "version": schema.version,
                   "fingerprint": hashlib.sha256(schema.model_dump_json(by_alias=True).encode()).hexdigest()[:12]},
    }


class QueryGraphRunner:
    """Runs turns of a conversation. A paused turn returns a state with `clarification` (what to
    ask) and `thread_id` (where to resume)."""

    def __init__(self, dependencies: QueryGraphDependencies, checkpointer=None):
        self._checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self._graph = build_query_graph(dependencies, self._checkpointer)
        self.dependencies = dependencies
        self.run_info = _run_info(dependencies)

    def invoke(
        self,
        *,
        question: str,
        query_id: str,
        explicit_intent: SemanticIntent | None = None,
        thread_id: str | None = None,
        ephemeral: bool | None = None,
    ) -> AgentState:
        """Run one turn. An ephemeral thread (the default without a thread id) is deleted once the
        turn is answered; a conversation thread is kept for follow-ups."""
        thread = thread_id or f"{_SINGLE_PREFIX}{query_id}"
        state = self._graph.invoke(
            build_initial_state(question=question, query_id=query_id, explicit_intent=explicit_intent),
            self._config(thread),
        )
        return self._finish(state, thread, thread_id is None if ephemeral is None else ephemeral)

    def resume(self, *, thread_id: str, answer: Any, ephemeral: bool | None = None) -> AgentState:
        """Continue a paused turn with the user's answer to its clarification."""
        if self.pending_clarification(thread_id) is None:
            raise NoPendingClarificationError("NO_PENDING_CLARIFICATION")
        state = self._graph.invoke(Command(resume=answer), self._config(thread_id))
        return self._finish(state, thread_id, thread_id.startswith(_SINGLE_PREFIX) if ephemeral is None else ephemeral)

    def stream(
        self,
        *,
        thread_id: str,
        ephemeral: bool,
        question: str | None = None,
        query_id: str | None = None,
        explicit_intent: SemanticIntent | None = None,
        answer: Any = None,
        resume: bool = False,
    ) -> Iterator[tuple[str, Any]]:
        """Run a turn (or resume one) step by step. Yields ("step", (node, update)) after each
        node, then ("done", final state) as `invoke`/`resume` would return it."""
        if resume:
            if self.pending_clarification(thread_id) is None:
                raise NoPendingClarificationError("NO_PENDING_CLARIFICATION")
            graph_input: Any = Command(resume=answer)
        else:
            graph_input = build_initial_state(question=question or "", query_id=query_id or "",
                                              explicit_intent=explicit_intent)
        config = self._config(thread_id)
        for chunk in self._graph.stream(graph_input, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node != "__interrupt__":
                    yield "step", (node, update or {})
        snapshot = self._graph.get_state(config)
        state = dict(snapshot.values)
        interrupts = [pending for task in snapshot.tasks for pending in task.interrupts]
        if interrupts:
            state["__interrupt__"] = interrupts
        yield "done", self._finish(state, thread_id, ephemeral)

    def pending_clarification(self, thread_id: str) -> dict | None:
        snapshot = self._graph.get_state(self._config(thread_id))
        for task in snapshot.tasks:
            for pending in task.interrupts:
                return pending.value
        return None

    def forget(self, thread_id: str) -> None:
        self._checkpointer.delete_thread(thread_id)

    def _finish(self, state: dict, thread: str, ephemeral: bool) -> AgentState:
        interrupts = state.pop("__interrupt__", None)
        if interrupts:
            state["clarification"] = interrupts[0].value
            state["thread_id"] = thread
        elif ephemeral:
            self.forget(thread)
        return state  # type: ignore[return-value]

    @staticmethod
    def _config(thread: str) -> dict:
        return {"configurable": {"thread_id": thread}}
