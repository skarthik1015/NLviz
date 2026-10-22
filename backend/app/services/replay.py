"""Rewrite replay for the ablation (docs/ablation-results.md): the LLM proposals recorded in a run
(audit "proposal" steps) go through the agent again with the phrase rules on or off. The model's
output is the same on both sides, so the difference is what the rules do, including what follows
them (validation, clarifications, compilation). Served by /bench/replay (BENCH_ENDPOINTS=on only).
"""
from __future__ import annotations

import dataclasses
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import QueryGraphRunner
from app.llm import usage_meter
from app.models import SemanticIntent
from app.services.intent_mapper import IntentProposal
from app.services.query_service import QueryService

MAX_CLARIFICATIONS = 3


class ReplayMapper:
    """Answers each mapping attempt with the next recorded proposal (the last one again when they run
    out); everything else (value index, fallback) is the connection's own mapper."""

    def __init__(self, mapper, proposals: list[dict], phrase_rewrites: bool):
        self._mapper = mapper
        self._proposals = list(proposals)
        self.config = dataclasses.replace(mapper.config, phrase_rewrites=phrase_rewrites)

    def load(self, proposals: list[dict]) -> None:
        self._proposals = list(proposals)

    def propose(self, question, registry, schema, *, context=None, cache=None) -> IntentProposal:
        del question, registry, schema, context, cache
        step = self._proposals.pop(0) if len(self._proposals) > 1 else (self._proposals[0] if self._proposals else {})
        intent = SemanticIntent.model_validate(step["intent"]) if step.get("intent") else None
        hints = step.get("value_hints")
        return IntentProposal(intent=intent, source="llm", error=step.get("error"), trace=["Intent mapper: replay"],
                              value_hints=hints, examples=None)

    def __getattr__(self, name: str):
        return getattr(self._mapper, name)


def replay_case(runtime, turns: list[dict], phrase_rewrites: bool) -> list[dict[str, Any]]:
    """`turns`: [{question, proposals: [proposal steps in order], answers: [clarification replies]}].
    One conversation; returns per turn {"first": response, "final": response} as ChatResponse dicts."""
    dependencies = runtime.query_graph.dependencies
    mapper = ReplayMapper(dependencies.intent_mapper, [], phrase_rewrites)
    runner = QueryGraphRunner(dataclasses.replace(dependencies, intent_mapper=mapper), InMemorySaver())
    service = QueryService(runner)
    results = []
    for turn in turns:
        mapper.load(turn.get("proposals") or [])
        with usage_meter():
            first = service.run_question(turn["question"], debug=True, conversation_id="replay")
            final, answers = first, list(turn.get("answers") or [])[:MAX_CLARIFICATIONS]
            while final.status == "clarification" and answers:
                final = service.resume("replay", answers.pop(0), debug=True)
        results.append({"first": first.model_dump(mode="json"), "final": final.model_dump(mode="json")})
    return results
