"""Understanding the question: propose an intent, check it, repair it, or ask the user.

map_intent ─▶ check_intent ─▶ (compile)
     ▲             │  │
     └── repair ───┘  └─▶ clarify ─▶ check_intent

- map_intent asks the mapper for an intent. The LLM sees the previous turn of the conversation
  (for follow-ups) and, on a repair, why its last answer was rejected.
- check_intent validates against the registry and snaps filter values to real values. An invalid
  LLM intent goes back to the LLM with the error (at most MAX_REPAIRS times per turn) before the
  heuristic fallback. A value with no confident match but close candidates pauses for the user.
- clarify pauses the graph (LangGraph interrupt) until the user picks a value.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from langgraph.types import interrupt

from app.agent.ambiguity import Probe, apply_choice, find_ambiguity
from app.agent.state import MAX_REPAIRS, AgentState
from app.connectors.base import SchemaContext
from app.semantic import SemanticRegistry
from app.services.intent_mapper import (IntentMapperRouter, IntentMappingError, IntentValidationError,
                                         ranking_request, validate_semantic_intent)

logger = logging.getLogger(__name__)

# The answer that drops the filter instead of choosing a value.
ANY_VALUE = "__any__"


def _describe(intent) -> str:
    dims = ", ".join(d.replace("_", " ") for d in intent.dimensions)
    return f"Understood: {intent.metric.replace('_', ' ')}" + (f" by {dims}" if intent.dimensions else "")


def _intent_json(intent) -> str:
    return json.dumps(intent.model_dump(mode="json", exclude_none=True), separators=(",", ":"))


def _dump(intent) -> dict | None:
    """An intent as recorded in the audit trail."""
    return intent.model_dump(mode="json", exclude_none=True) if intent is not None else None


def _context(state: AgentState) -> str | None:
    parts = []
    previous = state.get("previous_turn")
    if previous:
        parts.append(
            f"Previous question in this conversation: {previous['question']}\n"
            f"Its intent: {json.dumps(previous['intent'], separators=(',', ':'))}\n"
            "If the new question is a follow-up that refers to the previous one (for example 'now by month', "
            "'only credit card payments', 'what about RJ?', 'and the average?'), start from that intent and change "
            "only what the new question asks for. If the new question stands on its own, ignore the previous one."
        )
    if state.get("repair_feedback"):
        parts.append(state["repair_feedback"])
    return "\n".join(parts) or None


def build_map_intent_node(
    intent_mapper: IntentMapperRouter,
    registry: SemanticRegistry,
    schema: SchemaContext,
) -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        if state.get("explicit_intent") is not None:
            return {"intent": state["explicit_intent"], "intent_source": "explicit", "mapping_error": None,
                    "execution_error": None,
                    "audit": [{"stage": "proposal", "source": "explicit", "intent": _dump(state["explicit_intent"])}]}

        question = state.get("question", "").strip()
        if not question:
            raise ValueError("Question is required before intent mapping")

        cache = state.get("grounding_cache") if state.get("repair_count") else None
        proposal = intent_mapper.propose(question, registry, schema, context=_context(state), cache=cache)
        update: AgentState = {
            "intent": proposal.intent,
            "intent_source": proposal.source,
            "mapping_error": proposal.error,
            "execution_error": None,
            "debug_trace": proposal.trace,
            "grounding_cache": cache or {"hints": proposal.value_hints, "examples": proposal.examples},
        }
        proposed = {"stage": "proposal", "attempt": state.get("repair_count", 0), "source": proposal.source,
                    "intent": _dump(proposal.intent), "error": proposal.error}
        if cache is None:  # looked up on the first attempt; repairs reuse them
            proposed["value_hints"] = proposal.value_hints or []
            proposed["examples"] = [e["question"] for e in proposal.examples or ()]
        update["audit"] = [proposed]
        if proposal.value_hints and not state.get("repair_count"):
            update["evidence"] = [{"kind": "hint", **hint} for hint in proposal.value_hints]
        return update

    return node


def build_check_intent_node(
    intent_mapper: IntentMapperRouter,
    registry: SemanticRegistry,
    schema: SchemaContext,
    probe: Probe | None = None,
) -> Callable[[AgentState], AgentState]:
    """`probe` compiles and runs an intent; ambiguity checks use it (none: no checks)."""
    rewrites_on = getattr(getattr(intent_mapper, "config", None), "phrase_rewrites", True)

    def node(state: AgentState) -> AgentState:
        intent = state.get("intent")
        source = state.get("intent_source", "")
        error = state.get("mapping_error")
        trace: list[str] = []
        audit: list[dict] = []
        if intent is not None and intent.unsupported and source == "llm":
            # Answering with the closest metric would be a confident wrong answer; say what is missing.
            return {"unsupported": intent.unsupported.strip()[:120], "repair_feedback": None,
                    "pending_clarification": None, "refused_by": "answerability",
                    "user_trace": [f"Not in this data: {intent.unsupported.strip()[:120]}"],
                    "debug_trace": [f"Check intent: unsupported ({intent.unsupported[:120]})"],
                    "audit": [{"stage": "refused", "by": "answerability", "reason": intent.unsupported.strip()[:120]}]}
        if intent is not None and not error:
            try:
                intent = validate_semantic_intent(intent, registry, schema)
            except (IntentValidationError, ValueError, KeyError) as exc:
                error = str(exc)

        if error:
            if source == "explicit" or source == "heuristic":
                raise IntentValidationError(error)
            repairs = state.get("repair_count", 0)
            if repairs < MAX_REPAIRS:
                rejected = f" Rejected intent: {_intent_json(intent)}." if intent is not None else ""
                return {
                    "repair_feedback": (
                        f"Your previous answer to this question was rejected: {error[:500]}.{rejected} "
                        "Answer again, using only metrics, dimensions and operators from the registry."
                    ),
                    "repair_count": repairs + 1,
                    "user_trace": [f"Rechecking the interpretation (attempt {repairs + 1})"],
                    "debug_trace": [f"Check intent: rejected ({error[:300]}); repair {repairs + 1}/{MAX_REPAIRS}"],
                    "audit": [{"stage": "rejected", "attempt": repairs, "error": error[:500]}],
                }
            try:
                fallback = intent_mapper.fallback(state.get("question", ""), registry, schema, error)
            except IntentMappingError:
                if not getattr(getattr(intent_mapper, "config", None), "sql_fallback", False):
                    raise
                # No valid reading: the SQL fallback tries; if it cannot either, the 422 stands.
                from app.models import SemanticIntent

                return {"unsupported": "QUESTION_NOT_UNDERSTOOD", "refused_by": "not_understood",
                        "intent": state.get("intent") or SemanticIntent(metric="unknown"), "repair_feedback": None,
                        "pending_clarification": None, "debug_trace": ["Check intent: no valid reading"],
                        "audit": [{"stage": "refused", "by": "not_understood", "reason": error[:300]}]}
            intent, source = fallback.intent, fallback.source
            trace += fallback.trace
            audit.append({"stage": "fallback", "reason": error[:300], "intent": _dump(intent)})

        update: AgentState = {"intent": intent, "intent_source": source, "repair_feedback": None,
                              "mapping_error": None, "pending_clarification": None}
        index = intent_mapper.value_index() if source != "explicit" else None
        rewrites = _phrase_rewrites(state, registry, schema, index) if source != "explicit" and rewrites_on else {}
        if "aggregation_word_filters" in rewrites:
            # Before snapping too: "avg rating" would otherwise snap rating_text to 'Average'.
            update["intent"] = _rewrite("aggregation_word_filters", rewrites["aggregation_word_filters"],
                                        update["intent"], trace, audit)
            intent = update["intent"]
        if index is not None and intent.filters:
            from app.grounding.values import snap_intent  # lazy: app.grounding imports app.services

            try:
                outcome = snap_intent(intent, index, question=state.get("question"), confirmed=_confirmed(state))
            except Exception as exc:  # grounding must never break a question
                logger.warning("Value snapping failed: %s", exc)
            else:
                update["intent"] = outcome.intent
                update["evidence"] = [{"kind": "match", **item} for item in outcome.evidence]
                trace += outcome.trace
                if outcome.intent != intent:
                    audit.append({"stage": "snap", "before": _dump(intent), "after": _dump(outcome.intent)})
                if outcome.unresolved:
                    first = outcome.unresolved[0]
                    label = first.dimension.replace("_", " ")
                    prompt = (f"Which {label} did you mean? I assumed {first.raw}." if first.reason == "assumed"
                              else f"Which {label} did you mean by “{first.raw}”?")
                    update["pending_clarification"] = {
                        "kind": "value",
                        "reason": first.reason,
                        "prompt": prompt,
                        "dimension": first.dimension,
                        "raw": first.raw,
                        "filter_index": first.filter_index,
                        "options": first.options,
                        "any_value": ANY_VALUE,
                    }
                    audit.append({"stage": "clarify", "kind": "value", "reason": first.reason,
                                  "dimension": first.dimension, "raw": first.raw})
        before_rules = update["intent"]
        if not update.get("pending_clarification") and rewrites:
            # Again on the snapped values: "avg rating" may have snapped to rating_text = 'Average'.
            for name, rewrite in rewrites.items():
                update["intent"] = _rewrite(name, rewrite, update["intent"], trace, audit)
            if update["intent"] != before_rules:
                # The rules each edit one part; the whole must still be valid. Only some of them
                # checked their own result (review), so the final intent is validated once here.
                try:
                    update["intent"] = validate_semantic_intent(update["intent"], registry, schema)
                except (IntentValidationError, ValueError, KeyError) as exc:
                    trace.append(f"Check intent: the rules' edits did not validate ({str(exc)[:200]}); "
                                 "kept the intent as mapped")
                    audit.append({"stage": "rewrites_reverted", "error": str(exc)[:300]})
                    update["intent"] = before_rules
        if not update.get("pending_clarification"):
            # Every source, explicit intents included: a request body must not be a way around the
            # double-counting and mixed-unit checks (review B6).
            answered = frozenset(item["text"].replace(" ", "_") for item in state.get("evidence") or ()
                                 if isinstance(item, dict) and item.get("kind") == "clarification" and item.get("text"))
            checked = update["intent"]
            pending, notes, note, update["intent"] = find_ambiguity(update["intent"], registry, probe, answered)
            trace += note
            if update["intent"] != checked:
                audit.append({"stage": "ambiguity", "before": _dump(checked), "after": _dump(update["intent"])})
            if pending and pending.get("kind") == "refuse":
                update["unsupported"] = pending["text"]
                update["refused_by"] = "ambiguity"
                update["user_trace"] = [f"Not answerable this way: {pending['text']}"]
                update["debug_trace"] = [f"Check intent: refused ({pending['reason']})", *trace]
                update["audit"] = [*audit, {"stage": "refused", "by": "ambiguity", "reason": pending["reason"]}]
                return update
            if pending:
                update["pending_clarification"] = pending
                audit.append({"stage": "clarify", "kind": pending.get("kind"), "reason": pending.get("reason")})
            if notes:
                update["evidence"] = [*update.get("evidence", []), *notes]
        if source == "llm_fallback" and not update.get("pending_clarification"):
            # The keyword fallback answered "albums with more than 5 tracks" as the 100 albums with the
            # most tracks, silently. Say it was a simpler reading, and what was computed.
            from app.agent.nodes.explainer import describe_computed

            update["evidence"] = [*update.get("evidence", []), {
                "kind": "note", "dimension": "fallback",
                "text": f"I couldn't fully interpret the question, so this answers a simpler reading: "
                        f"{describe_computed(update['intent'], registry)}. Parts of the question may not be reflected."}]
        update["user_trace"] = [_describe(update["intent"])]
        update["debug_trace"] = [f"Check intent: source={source}, metric={update['intent'].metric}", *trace]
        update["audit"] = [*audit, {"stage": "checked", "source": source, "intent": _dump(update["intent"])}]
        return update

    return node


# The check rules that react to the question's wording, in the order they run. They correct the
# LLM's intent ("how many" asks for a count, "top 5" sets the limit, ...). PHRASE_REWRITES=off in
# the environment turns all of them off, for measuring what they add (docs/ablation-results.md).
# Everything else stays on: validation and repair, value snapping and value clarifications, the
# ambiguity checks (double counting, mixed units, zeros), the answerability check, the keyword
# fallback after failed repairs, the SQL safety gate and the engine guards.
PHRASE_REWRITES = (
    "aggregation_word_filters",
    "how_many_counts",
    "entity_name_breakdowns",
    "unasked_average",
    "filter_named_values",
    "unasked_breakdowns",
    "drop_redundant_breakdown",
    "flag_filters",
    "per_entity_average",
    "narrowing_follow_up",
    "measure_swap_follow_up",
    "terse_follow_up",
    "requested_ranking",
)


def _phrase_rewrites(state: AgentState, registry, schema, index) -> dict[str, Callable]:
    """PHRASE_REWRITES bound to this turn: name -> rewrite(intent) -> (intent, notes)."""
    question = state.get("question", "")
    evidence = state.get("evidence") or ()
    previous = (state.get("previous_turn") or {}).get("intent")
    hinted = [item["dimension"] for item in evidence if isinstance(item, dict) and item.get("kind") == "hint"]

    def unasked_breakdowns(intent):
        # After the named-value rule: "orders from Brasília" grouped by customer_state becomes the
        # filter state = DF there; dropped first, the place was lost (grounded eval, run 7).
        inherited = (previous or {}).get("dimensions") or () if _is_follow_up(intent, question, previous) else ()
        return _unasked_breakdowns(intent, question, registry, [*inherited, *hinted])

    rewrites = {
        "aggregation_word_filters": lambda i: _aggregation_word_filters(i, question, registry),
        "how_many_counts": lambda i: _how_many_counts(i, question, registry),
        "entity_name_breakdowns": lambda i: _entity_name_breakdowns(i, question, registry),
        "unasked_average": lambda i: _unasked_average(i, question, registry),
        "filter_named_values": lambda i: _filter_named_values(i, question, registry, evidence),
        "unasked_breakdowns": unasked_breakdowns,
        "drop_redundant_breakdown": lambda i: _drop_redundant_breakdown(i, question, registry, _confirmed(state)),
        "flag_filters": lambda i: _flag_filters(i, question, registry, index),
        "per_entity_average": lambda i: _per_entity_average(i, question, registry),
        "narrowing_follow_up": lambda i: _narrowing_follow_up(i, question, registry, schema, previous),
        "measure_swap_follow_up": lambda i: _measure_swap_follow_up(i, question, registry, schema, previous),
        "terse_follow_up": lambda i: _terse_follow_up(i, question, registry, previous),
        "requested_ranking": lambda i: _requested_ranking(i, question),
    }
    assert tuple(rewrites) == PHRASE_REWRITES
    return rewrites


def _rewrite(name: str, rewrite: Callable, intent, trace: list[str], audit: list[dict]):
    """Applies one rule; a change is recorded with the intent before and after."""
    changed, notes = rewrite(intent)
    trace += notes
    if changed != intent:
        audit.append({"stage": "rewrite", "rule": name, "before": _dump(intent), "after": _dump(changed)})
    return changed

def _asks_breakdown(question: str, dimension) -> bool:
    """Whether the question asks to break down by the dimension: "by order status", "per state",
    "for each category", "split by region" (the dimension's label, its name, or its last word)."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    folded = f" {fold(question)} "
    names = {fold(dimension.display_name), fold(dimension.name.replace("_", " ")),
             fold(dimension.name.split("_")[-1])}
    # "by cuisine" for a dimension named "cuisines".
    names |= {name[:-1] for name in names if len(name) > 3 and name.endswith("s")}
    return any(f" {lead} {name} " in folded or f" {lead} {name}s " in folded
               for lead in ("by", "per", "each", "every", "across") for name in names if name)


def build_unsupported_node(registry: SemanticRegistry) -> Callable[[AgentState], AgentState]:
    """Ends a turn the data cannot answer: what is missing, and what can be asked instead."""

    def node(state: AgentState) -> AgentState:
        schema = registry.schema
        metrics = ", ".join(m.display_name for m in schema.metrics[:8])
        dimensions = ", ".join(d.display_name for d in schema.dimensions[:8])
        times = ", ".join(t.display_name for t in schema.time_dimensions[:3])
        missing = state["unsupported"].strip()
        # The answerability check names what is missing ("number of tracks"); the double-counting and
        # unit checks give a whole sentence, which was wrapped as "This data has no <sentence>".
        reason = missing if missing.endswith(".") else f"This data has no {missing}, so I can't answer that without guessing."
        explanation = (f"{reason} "
                       f"You can ask about {metrics}" + (f", broken down by {dimensions}" if dimensions else "")
                       + (f", or over time by {times}" if times else "") + ".")
        return {"explanation": explanation, "sql": None, "rows": [], "row_count": 0, "chart_spec": None,
                "user_trace": ["Explained what this data can answer"]}

    return node


def _part_of_its_name(value, dimension) -> bool:
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    word = fold(str(value)).replace(" ", "")
    own = "".join(fold(f"{dimension.name} {dimension.display_name}").replace("_", " ").split())
    return len(word) >= 3 and word in own


def _filter_named_values(intent, question: str, registry, evidence):
    """A value the question names word for word, whose dimension the LLM grouped by instead of
    filtering: "How many Asian restaurants are in New Delhi?" came back grouped by city, with no
    city filter. The breakdown becomes a filter unless the question asks for it ("by city").
    A filter the LLM left out entirely is added only for a dimension of names (not low
    cardinality: "Average" is also a rating label) whose value no other filter or dimension uses."""
    from app.models.semantic_intent import FilterCondition

    filtered = {condition.dimension for condition in intent.filters}
    named: dict[str, list[str]] = {}
    for item in evidence:
        if isinstance(item, dict) and item.get("kind") == "hint" and item.get("exact"):
            named.setdefault(item["dimension"], []).append(item["value"])
    dimensions, filters, notes = list(intent.dimensions), list(intent.filters), []
    used = {str(v) for c in intent.filters for v in (c.value if isinstance(c.value, list) else [c.value])}
    owners: dict[str, int] = {}
    for values in named.values():
        for value in set(values):
            owners[value] = owners.get(value, 0) + 1
    # "How many invoices came from Brazil?": Brazil is a billing country and a customer country, so
    # neither filter was added. The metric's own table settles it (invoices carry billing_country).
    home = registry.get_metric(intent.metric).required_tables[:1]
    at_home = {}
    for dimension, values in named.items():
        if len(values) == 1 and registry.get_dimension(dimension).required_tables[:1] == home:
            at_home.setdefault(values[0], []).append(dimension)
    for dimension, values in named.items():
        if dimension in filtered or len(values) != 1 or _asks_breakdown(question, registry.get_dimension(dimension)):
            continue
        if _part_of_its_name(values[0], registry.get_dimension(dimension)):
            # "Average discount by ship mode": the value SHIP is a word of l_shipmode's own name, so
            # the question names the column, not the value. Every harmful firing on the benchmark
            # was this (docs/ablation-results.md); the rule's other uses (Gurgaon, Canada) stay.
            continue
        if dimension in dimensions:
            dimensions.remove(dimension)
            notes.append(f"Check intent: {dimension} = '{values[0]}' is named in the question; filter instead of breakdown")
        elif ((registry.get_dimension(dimension).cardinality != "low" or registry.get_dimension(dimension).separator)
              and values[0] not in used and (owners[values[0]] == 1 or at_home.get(values[0]) == [dimension])):
            # A list item named word for word is a concrete thing ("films with Deleted Scenes"), never a
            # label that doubles as an aggregation word, whatever the list's cardinality.
            # Dropped altogether ("Top cuisines in Gurgaon by votes" lost the city). Only names
            # (cities, categories), never a low-cardinality label: "Average" is also a rating label.
            notes.append(f"Check intent: {dimension} = '{values[0]}' is named in the question; added the filter")
        else:
            continue
        filters.append(FilterCondition(dimension=dimension, operator="eq", value=values[0]))
    if not notes:
        return intent, []
    return intent.model_copy(update={"dimensions": dimensions, "filters": filters}), notes


_AGGREGATION_WORDS = {"average", "mean", "total", "sum", "count", "median", "minimum", "maximum"}


def _aggregation_word_filters(intent, question: str, registry):
    """A filter on a value that is an aggregation word the question only uses for the metric:
    "Average rating of restaurants with table booking" came back with rating_text = 'Average'.
    Kept when the word stands on its own ("restaurants rated Average")."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    words = fold(question).split()
    metric = registry.get_metric(intent.metric)
    metric_words = set(fold(f"{metric.display_name} {metric.name.replace('_', ' ')}").split()) - _AGGREGATION_WORDS
    kept, notes = [], []
    for condition in intent.filters:
        value = fold(str(condition.value)) if isinstance(condition.value, str) else None
        at = [i for i, word in enumerate(words) if word == value]
        if value in _AGGREGATION_WORDS and at and all(i + 1 < len(words) and words[i + 1] in metric_words for i in at):
            notes.append(f"Check intent: '{condition.value}' names the aggregation, not a {condition.dimension}; "
                         "dropped the filter")
            continue
        kept.append(condition)
    if not notes:
        return intent, []
    return intent.model_copy(update={"filters": kept}), notes


_POSITIVE = {"yes", "true", "y"}
_NEGATIVE = {"no", "false", "n"}
_COMPARISON = re.compile(r"\b(vs|versus|compare|compared|comparison|with and without|or not|share|percent|"
                         r"percentage|proportion|split|ratio|breakdown|whether)\b")
# Folding turns "don't" into "don t".
_NEGATION = re.compile(r"(\bnot\b|n'?\s?t\b|\bwithout\b|\bno\b|\bnever\b)")


def _flag_filters(intent, question: str, registry, index):
    """A yes/no dimension the question states rather than compares: "How many restaurants offer
    table booking?" was grouped into Yes and No rows; it asks for the Yes count. The breakdown
    becomes a filter on the positive value (the negative one for "without", "don't", "not"),
    unless the question asks for the split ("by", "vs", "with and without", "share")."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services
    from app.models.semantic_intent import FilterCondition

    if index is None or not hasattr(index, "values"):
        return intent, []
    folded = f" {fold(question)} "
    if _COMPARISON.search(folded):
        return intent, []
    filtered = {c.dimension for c in intent.filters}
    dimensions, filters, notes = list(intent.dimensions), list(intent.filters), []
    eligible = []
    for dimension in intent.dimensions:
        values = {fold(v): v for v in index.values(dimension)}
        positive = next((values[v] for v in values if v in _POSITIVE), None)
        negative = next((values[v] for v in values if v in _NEGATIVE), None)
        if (positive is None or negative is None or len(values) != 2 or dimension in filtered
                or _asks_breakdown(question, registry.get_dimension(dimension))):
            continue
        eligible.append((dimension, positive, negative))
    for dimension, positive, negative in eligible:
        if len(eligible) == 1:
            negated = bool(_NEGATION.search(folded))
        else:
            # Several flags: each takes the polarity of its own mention ("without online delivery
            # that offer table booking" is No and Yes); one negation anywhere made both No (review).
            negated = _negated_mention(folded, registry.get_dimension(dimension))
            if negated is None:  # not named in the question: left to the breakdown
                continue
        value = negative if negated else positive
        dimensions.remove(dimension)
        filters.append(FilterCondition(dimension=dimension, operator="eq", value=value))
        notes.append(f"Check intent: {dimension} is a yes/no the question states; filter {dimension} = '{value}'")
    if not notes:
        return intent, []
    return intent.model_copy(update={"dimensions": dimensions, "filters": filters}), notes




_FLAG_FILLER = {"has", "have", "is", "are", "offers", "offer", "with", "flag", "available"}


def _negated_mention(folded: str, dimension) -> bool | None:
    """Whether the words just before this flag's mention negate it; None when it is not mentioned."""
    from app.grounding.text import fold

    words = [w for w in fold(f"{dimension.name.replace('_', ' ')} {dimension.display_name}").split()
             if len(w) > 2 and w not in _FLAG_FILLER]
    positions = [m.start() for w in dict.fromkeys(words) for m in re.finditer(r'\b' + re.escape(w) + r'\b', folded)]
    if not positions:
        return None
    before = " ".join(folded[:min(positions)].split()[-4:])
    return bool(_NEGATION.search(f" {before} "))


def _per_entity_average(intent, question: str, registry):
    """ "Average votes per restaurant by rating text" came back broken down by restaurant name too:
    an average over each restaurant's own row, one row per restaurant. "Per <entity>" with an
    average names the unit averaged over, not a breakdown by the entity's name dimension."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    if registry.get_metric(intent.metric).aggregation != "AVG" or not intent.dimensions:
        return intent, []
    folded = f" {fold(question)} "
    kept, notes = [], []
    for name in intent.dimensions:
        dimension = registry.get_dimension(name)
        stem = re.sub(r"_?(name|title)$", "", name)
        if (stem != name and stem and dimension.cardinality == "high"
                and (f" per {stem} " in folded or f" per {stem.replace('_', ' ')} " in folded)):
            notes.append(f"Check intent: 'per {stem}' is the unit averaged over; dropped the {name} breakdown")
            continue
        kept.append(name)
    return (intent.model_copy(update={"dimensions": kept}), notes) if notes else (intent, [])


_HOW_MANY = re.compile(r"\b(?:how many|number of|count of) ([a-z]+)")
_COUNTS = ("COUNT", "COUNT_DISTINCT")


def _how_many_counts(intent, question: str, registry):
    """ "How many invoices came from Brazil?" came back as the sum of invoice amounts (qwen3:4b, on
    both engines), and "Number of orders by status" as a count of order lines (Northwind, run 8).
    "How many <things>" / "number of <things>" asks for a count of those things: the metric becomes
    the one count named after them; with several, the one named by the noun alone ("invoices", not
    "invoice lines"). A count already named by the noun alone stays, and "how many units" (a sum of
    a quantity) names no count and is left alone."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    match = _HOW_MANY.search(fold(question))
    if match is None:
        return intent, []
    noun = match.group(1)
    forms = {noun, noun[:-1] if noun.endswith("s") else noun, re.sub(r"ies$", "y", noun)}
    generic = {"total", "number", "of", "count", "unique", "distinct", "num"}

    def words(m) -> set[str]:
        return set(fold(f"{m.display_name} {m.name.replace('_', ' ')}").split())

    current = registry.get_metric(intent.metric)
    named = [m for m in registry.schema.metrics if m.aggregation in _COUNTS and forms & words(m)]
    pure = [m for m in named if words(m) - generic <= forms]
    if current.aggregation in _COUNTS:
        if current in pure or len(pure) != 1:
            return intent, []
        counts = pure
    else:
        counts = named if len(named) == 1 else pure
    if len(counts) != 1 or counts[0].name == intent.metric:
        return intent, []
    return (intent.model_copy(update={"metric": counts[0].name}),
            [f"Check intent: '{match.group(0)}' asks for a count of {noun}; metric {counts[0].name} instead of {intent.metric}"])


_AVERAGE_WORDS = re.compile(r"\b(average|averages|avg|mean|typical|typically|per|median)\b")
_SUM_WORDS = re.compile(r"\b(revenue|revenues|sales|total|totals|sum|spend|spent|spending|earned|earnings|income|"
                        r"turnover|how much)\b")


def _unasked_average(intent, question: str, registry):
    """ "Revenue by month" came back as the average payment (Pagila, qwen3:4b: the sum was named
    total_payment_amount, with no "revenue" in it). An average the question never asks for, where
    a sum of the same column exists, becomes that sum."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    metric = registry.get_metric(intent.metric)
    folded = fold(question)
    # A sum word is required, and a column that is itself an average never qualifies: "cost for two in
    # Delhi" must stay the average of average_cost_for_two (summing it gave the 11.45M of Phase 4).
    if (metric.aggregation != "AVG" or _AVERAGE_WORDS.search(folded) or not _SUM_WORDS.search(folded)
            or re.search(r"(^|[._])(avg|average|mean)(_|$)", (metric.sql_expression or "").lower())):
        return intent, []
    expression = (metric.sql_expression or "").replace(" ", "")
    sums = [m for m in registry.schema.metrics if m.aggregation == "SUM" and not m.base_filter
            and (m.sql_expression or "").replace(" ", "") == expression and metric.base_filter is None]
    if len(sums) != 1:
        return intent, []
    return (intent.model_copy(update={"metric": sums[0].name}),
            [f"Check intent: the question asks for no average; {sums[0].name} instead of {intent.metric}"])


def _entity_name_breakdowns(intent, question: str, registry):
    """ "Sales by employee" came back by job title (Northwind, qwen3:4b): "by <table>" means the rows
    of that table, so a breakdown by another of its columns that the question does not name becomes
    the table's name (a person's full name first)."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    folded = f" {fold(question)} "
    dimensions, notes = list(intent.dimensions), []
    for i, name in enumerate(dimensions):
        dimension = registry.get_dimension(name)
        if len(dimension.required_tables) != 1 or _asks_breakdown(question, dimension):
            continue
        table = dimension.required_tables[0]
        singular = re.sub(r"ies$", "y", re.sub(r"(?<=[^su])s$", "", table.lower()))
        own = set(name.split("_")) - set(table.lower().split("_")) - {singular}
        if any(f" {word}" in folded for word in own if len(word) > 2):
            continue  # "sales by employee job title" names the column
        if not any(f" {lead} {singular}{plural} " in folded for lead in ("by", "per", "each", "every", "across")
                   for plural in ("", "s")):
            continue
        names = [d for d in registry.schema.dimensions if d.required_tables == [table] and d.name != name
                 and ("||" in d.sql_expression or re.search(r"(name|title|company)$", d.name))]
        names.sort(key=lambda d: "||" not in d.sql_expression)
        if names and names[0].name not in dimensions:
            dimensions[i] = names[0].name
            notes.append(f"Check intent: 'by {singular}' means each {singular}; {names[0].name} instead of {name}")
    return (intent.model_copy(update={"dimensions": dimensions}), notes) if notes else (intent, [])


_BREAKDOWN_CUE = re.compile(r"\b(by|per|each|every|across|which|top|bottom|breakdown|split|most|least|fewest|"
                            r"highest|lowest|compare|compared|versus|vs|distribution|grouped|group)\b")
_NAME_WORDS = {"name", "names", "title", "id", "code"}


def _unasked_breakdowns(intent, question: str, registry, inherited=()):
    """A breakdown by a dimension the question never mentions, in a question with no breakdown
    words at all: "How many invoices came from Brazil?" came back grouped by invoice line unit
    price. A breakdown under another name ("by region" for state) has a cue word and stays, and so
    does one carried over from the previous turn ("Only March 2006" after "sales by employee") or one
    whose value the question names ("from Brasília": customer_state)."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    folded = fold(question)
    if not intent.dimensions or _BREAKDOWN_CUE.search(folded):
        return intent, []
    words = folded.split()
    kept, notes = [], []
    for name in intent.dimensions:
        dimension = registry.get_dimension(name)
        if name in inherited:
            kept.append(name)
            continue
        own = set(fold(f"{dimension.display_name} {name.replace('_', ' ')}").split()) - _NAME_WORDS
        # The table's words name the rows, not the breakdown: "invoices" is no ask for invoice line unit price.
        own = own - {w for t in dimension.required_tables for w in t.lower().split("_")} or own
        if any(word.startswith(w) or w.startswith(word) for w in own if len(w) > 2 for word in words if len(word) > 2):
            kept.append(name)
            continue
        notes.append(f"Check intent: the question asks for no breakdown by {name}; dropped it")
    return (intent.model_copy(update={"dimensions": kept}), notes) if notes else (intent, [])


_TERSE_MAX_WORDS = 4
# Words that make a short question a follow-up rather than a new question ("Top 3 only", "Just
# 2017", "Now by month", "And for Canada?").
_FOLLOW_UP_CUE = re.compile(r"^(?:and|but|now|then|same|just|only|also|what about|how about)\b|"
                            r"\b(?:only|instead|too|as well|those|them|same)\b")


def _is_follow_up(intent, question: str, previous: dict | None) -> bool:
    """Whether this turn builds on the previous one: it keeps the previous metric, or it says so.
    Shortness alone is no proof: "How many tracks?" after "Revenue by country in 2023" is a new
    question and must not inherit 2023 (review)."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    if not previous or not previous.get("metric"):
        return False
    return intent.metric == previous["metric"] or bool(_FOLLOW_UP_CUE.search(fold(question).strip()))
_YEAR_ONLY = re.compile(r"^(?:just|only|for|in)?\s*(?:the\s+)?(?:year\s+)?((?:19|20)\d{2})\s*(?:only)?[.?!]?$")
_DATE_WORDS = re.compile(r"\b((?:19|20)\d{2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|"
                         r"april|june|july|august|september|october|november|december|q[1-4]|quarter|month|year|week|"
                         r"day|today|yesterday|last|this|since|until|before|after|all time|overall|ever)\b")


_NO_BREAKDOWN = re.compile(r"\b(total|overall|altogether|combined|in all|all together|grand total|sum)\b")


def _as_date(value):
    from datetime import date

    return date.fromisoformat(value) if isinstance(value, str) else value


_NARROWING = re.compile(r"^(?:but\s+|and\s+)?(?:only|just|excluding|except|limited to|restricted to)\b")
_MEASURE_WORDS = re.compile(r"\b(count|counts|number|how many|average|averages|avg|mean|median|total|totals|sum|"
                            r"share|percent|percentage|rate|ratio)\b")


def _narrowing_follow_up(intent, question: str, registry, schema, previous: dict | None):
    """ "Only for customers in Canada" after "Revenue by month" came back as the number of customers in
    Canada, with no months (Pagila, run 12, twice: the model named a metric "unique_customers", and
    "customers" pulled the answer to it). A follow-up that starts by narrowing ("only", "just",
    "excluding") and names no measure keeps the previous metric, time axis and breakdown, and adds
    its filters; the result must still validate."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services
    from app.services.intent_mapper import IntentValidationError, validate_semantic_intent

    folded = fold(question).strip()
    if not previous or not previous.get("metric") or not _NARROWING.match(folded) or _MEASURE_WORDS.search(folded):
        return intent, []
    if previous["metric"] not in {m.name for m in registry.schema.metrics}:
        return intent, []
    restore = {"metric": previous["metric"]}
    if not intent.time_dimension and previous.get("time_dimension"):
        restore.update(time_dimension=previous["time_dimension"], time_granularity=previous.get("time_granularity"),
                       order_by=previous.get("order_by") or intent.order_by)
    filtered = {f.dimension for f in intent.filters}
    kept = [d for d in previous.get("dimensions") or [] if d not in filtered]
    if not intent.dimensions and kept:
        restore["dimensions"] = kept
    if not intent.start_date and not intent.end_date and (previous.get("start_date") or previous.get("end_date")):
        restore.update(start_date=_as_date(previous.get("start_date")), end_date=_as_date(previous.get("end_date")))
    candidate = type(intent).model_validate({**intent.model_dump(), **restore})  # previous turn is JSON
    if candidate == intent:
        return intent, []
    try:
        candidate = validate_semantic_intent(candidate, registry, schema)
    except IntentValidationError:
        return intent, []
    return candidate, [f"Check intent: '{question.strip()}' narrows the previous question; kept "
                       + ", ".join(f"{k}={v}" for k, v in restore.items() if k in ("metric", "time_dimension", "dimensions"))]


# "... instead" at the end, not anywhere: "count orders instead of payments" is a whole new question.
_MEASURE_SWAP = re.compile(r"^(?:and\s+)?(?:what|how)\s+about\b|\binstead\s*[.?!]?$")


def _measure_swap_follow_up(intent, question: str, registry, schema, previous: dict | None):
    """ "What about the average length?" after "Number of films by rating" came back as one overall
    average (Pagila, runs 11-12), and "What about the average price instead?" the same on Chinook: the
    follow-up changes only the measure. A "what about ..." / "... instead" follow-up that names no
    breakdown keeps the previous breakdown, time axis, filters and dates; the result must validate."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services
    from app.services.intent_mapper import IntentValidationError, validate_semantic_intent

    folded = fold(question).strip()
    if not previous or not _MEASURE_SWAP.search(folded) or _NO_BREAKDOWN.search(folded):
        return intent, []
    if intent.dimensions or intent.time_dimension:
        return intent, []
    known = {d.name for d in registry.schema.dimensions}
    restore: dict = {}
    kept = [d for d in previous.get("dimensions") or [] if d in known]
    if kept:
        restore["dimensions"] = kept
    if previous.get("time_dimension") in {t.name for t in registry.schema.time_dimensions}:
        restore.update(time_dimension=previous["time_dimension"], time_granularity=previous.get("time_granularity"),
                       order_by=previous.get("order_by") or intent.order_by)
    if not intent.filters and previous.get("filters"):
        restore["filters"] = previous["filters"]
    if not intent.start_date and not intent.end_date and (previous.get("start_date") or previous.get("end_date")):
        restore.update(start_date=_as_date(previous.get("start_date")), end_date=_as_date(previous.get("end_date")))
    if not restore:
        return intent, []
    try:
        candidate = validate_semantic_intent(type(intent).model_validate({**intent.model_dump(), **restore}),
                                             registry, schema)
    except (IntentValidationError, ValueError):
        return intent, []
    return candidate, [f"Check intent: '{question.strip()}' changes only the measure; kept "
                       + ", ".join(sorted(k for k in restore if k in ("dimensions", "time_dimension", "filters")))]


def _terse_follow_up(intent, question: str, registry, previous: dict | None):
    """ "Top 3 only" after "Revenue in 2018 by product category" came back with no breakdown and no
    dates, twice in a row (qwen3:4b; the multi-turn eval's known miss). A follow-up of a few words
    that names no breakdown keeps the previous breakdown, one that names no date keeps the previous
    dates, and "Just 2017" sets that year (the same model dropped it)."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services
    from datetime import date

    folded = fold(question).strip()
    if not previous or len(folded.split()) > _TERSE_MAX_WORDS or not _is_follow_up(intent, question, previous):
        return intent, []
    update, notes = {}, []
    year = _YEAR_ONLY.match(folded)
    if year and (intent.start_date, intent.end_date) != (date(int(year.group(1)), 1, 1), date(int(year.group(1)), 12, 31)):
        update.update(start_date=date(int(year.group(1)), 1, 1), end_date=date(int(year.group(1)), 12, 31))
        notes.append(f"Check intent: '{question.strip()}' limits to {year.group(1)}")
    elif (not intent.start_date and not intent.end_date and (previous.get("start_date") or previous.get("end_date"))
          and not _DATE_WORDS.search(folded)):
        update.update(start_date=_as_date(previous.get("start_date")), end_date=_as_date(previous.get("end_date")))
        notes.append("Check intent: a short follow-up naming no dates keeps the previous dates")
    kept = [d for d in previous.get("dimensions") or [] if d in {x.name for x in registry.schema.dimensions}]
    if not intent.dimensions and kept and not _NO_BREAKDOWN.search(folded) and not any(_asks_breakdown(question, registry.get_dimension(d))
                                                  for d in kept):
        names_breakdown = any(word in folded.split() for d in registry.schema.dimensions
                              for word in d.name.split("_") if len(word) > 3)
        if not names_breakdown:
            update["dimensions"] = kept
            notes.append(f"Check intent: a short follow-up naming no breakdown keeps {', '.join(kept)}")
    return (intent.model_copy(update=update), notes) if update else (intent, [])


def _requested_ranking(intent, question: str):
    """An explicit "top 5" / "which 5 cities" sets the limit (the LLM returned every city), and
    "fewest/least/lowest/smallest/bottom" sorts ascending. Only for a breakdown without a time axis."""
    if not intent.dimensions or intent.time_dimension:
        return intent, []
    ranking = ranking_request(question)
    update, notes = {}, []
    if ranking.limit is not None and intent.limit != ranking.limit:
        update["limit"] = ranking.limit
        notes.append(f"Check intent: the question asks for {ranking.limit} row(s); limit {ranking.limit}")
    if ranking.ascending and intent.order_by == "metric_desc":
        update["order_by"] = "metric_asc"
        notes.append("Check intent: the question asks for the lowest values; ascending order")
    return (intent.model_copy(update=update), notes) if update else (intent, [])


def _drop_redundant_breakdown(intent, question: str, registry, confirmed: set[tuple[str, str]]):
    """A breakdown by a dimension that is also filtered to one value of it always has one row. Keep
    the half the question asks for: the breakdown when it says "by <dimension>" ("cancellation rate
    by order status" had gained order_status = canceled), else the filter ("health and beauty
    products" had also been grouped by category). Values carried from the previous turn are left
    alone. A filter whose value the question names is never dropped (review)."""
    from app.grounding.text import fold  # lazy: app.grounding imports app.services

    dimensions, filters, notes = list(intent.dimensions), list(intent.filters), []
    for condition in list(filters):
        value = condition.value[0] if isinstance(condition.value, list) and len(condition.value) == 1 else condition.value
        if (isinstance(value, list) and condition.operator == "in" and condition.dimension in dimensions
                and registry.get_dimension(condition.dimension).separator
                and not _asks_breakdown(question, registry.get_dimension(condition.dimension))):
            # Several items of a list dimension ("Japanese or Sushi", "coffee" -> Cafe, Coffee and
            # Tea) is one group of rows; a breakdown would list every cuisine those rows also have.
            dimensions.remove(condition.dimension)
            notes.append(f"Check intent: dropped breakdown by {condition.dimension} (filtered to {', '.join(value)})")
            continue
        # "contains" too: "average rating of fast food places" was also grouped by every cuisine list
        # containing "fast food" when one figure was asked for.
        if (condition.operator not in ("eq", "in", "contains") or isinstance(value, list)
                or condition.dimension not in dimensions
                or (condition.dimension, str(value)) in confirmed):
            continue
        if _asks_breakdown(question, registry.get_dimension(condition.dimension)):
            if fold(str(value)) in fold(question):
                # "Revenue for toys, broken down by category" asks for both: one row, as asked.
                continue
            filters.remove(condition)
            notes.append(f"Check intent: dropped filter {condition.dimension} = '{value}' "
                         f"(the question asks for a breakdown by it)")
        else:
            dimensions.remove(condition.dimension)
            notes.append(f"Check intent: dropped breakdown by {condition.dimension} (filtered to '{value}')")
    if not notes:
        return intent, []
    return intent.model_copy(update={"dimensions": dimensions, "filters": filters}), notes


def _confirmed(state: AgentState) -> set[tuple[str, str]]:
    """Values not to question again: chosen by the user this turn, or kept from the previous turn
    (a follow-up such as 'now by payment type' does not repeat the earlier filter)."""
    confirmed = {(item["dimension"], item["value"]) for item in state.get("evidence") or ()
                 if isinstance(item, dict) and item.get("kind") == "clarification" and item.get("value")}
    previous = (state.get("previous_turn") or {}).get("intent") or {}
    for condition in previous.get("filters") or ():
        values = condition.get("value") if isinstance(condition.get("value"), list) else [condition.get("value")]
        confirmed.update((condition["dimension"], str(v)) for v in values)
    return confirmed


def build_clarify_node() -> Callable[[AgentState], AgentState]:
    def node(state: AgentState) -> AgentState:
        pending = state["pending_clarification"]
        answer = interrupt(pending)  # the graph pauses here until resumed with the user's answer
        intent = state["intent"]
        if pending.get("kind") == "choice":
            intent, label = apply_choice(intent, pending, answer)
            return {
                "intent": intent,
                "pending_clarification": None,
                "user_trace": [f"Using your choice: {label}"],
                "debug_trace": [f"Clarify ({pending['reason']}): {label}"],
                "audit": [{"stage": "clarified", "reason": pending["reason"], "answer": answer, "intent": _dump(intent)}],
                "evidence": [{"kind": "clarification", "dimension": pending.get("dimension") or "metric",
                              "text": pending["reason"].replace("_", " "), "value": label}],
            }
        filters = list(intent.filters)
        condition = filters[pending["filter_index"]]
        raw = pending["raw"]
        if isinstance(condition.value, list):
            values = [v for v in condition.value if v != raw]
            if answer not in (None, ANY_VALUE):
                values.append(str(answer))
            filters[pending["filter_index"]] = condition.model_copy(update={"value": values})
            if not values:
                del filters[pending["filter_index"]]
        elif answer in (None, ANY_VALUE):
            del filters[pending["filter_index"]]
        else:
            filters[pending["filter_index"]] = condition.model_copy(update={"value": str(answer)})
        chosen = "any value" if answer in (None, ANY_VALUE) else f"'{answer}'"
        intent = intent.model_copy(update={"filters": filters})
        return {
            "intent": intent,
            "audit": [{"stage": "clarified", "reason": pending["reason"], "answer": answer, "intent": _dump(intent)}],
            "pending_clarification": None,
            "user_trace": [f"Using {chosen} for {pending['dimension'].replace('_', ' ')}"],
            "debug_trace": [f"Clarify: '{raw}' -> {chosen}"],
            "evidence": [{"kind": "clarification", "dimension": pending["dimension"], "text": raw,
                          "value": None if answer in (None, ANY_VALUE) else str(answer)}],
        }

    return node


def route_after_check(state: AgentState) -> str:
    if state.get("unsupported"):
        return "unsupported"
    if state.get("repair_feedback"):
        return "repair"
    if state.get("pending_clarification"):
        return "clarify"
    return "compile"

