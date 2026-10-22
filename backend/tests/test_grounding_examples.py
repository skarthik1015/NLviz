from __future__ import annotations

from collections import Counter
from pathlib import Path

from app.grounding.examples import (ExampleIndex, ExampleIndexBuilder, examples_table, remove_example_sources,
                                    synthesize_examples)
from app.models import SemanticIntent
from app.semantic import build_sql_from_intent, load_semantic_registry
from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter, _build_user_prompt
from test_grounding_values import FakeRetrieval, _schema

REGISTRY = load_semantic_registry(Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml")


def test_every_example_is_a_valid_intent_that_compiles():
    examples = synthesize_examples(REGISTRY)
    for example in examples:
        build_sql_from_intent(SemanticIntent.model_validate(example["intent"]), REGISTRY)
    assert len({e["question"] for e in examples}) == len(examples)


def test_examples_cover_every_metric_and_shape_without_one_dominant_dimension():
    examples = synthesize_examples(REGISTRY)
    assert {e["intent"]["metric"] for e in examples} == {m.name for m in REGISTRY.schema.metrics}
    shapes = Counter(
        ("trend" if e["intent"]["time_dimension"] else "plain")
        + ("+dim" if e["intent"]["dimensions"] else "")
        + (":" + e["intent"]["order_by"])
        for e in examples
    )
    assert {"plain:metric_desc", "plain+dim:metric_desc", "plain+dim:metric_asc", "trend:time_asc",
            "trend+dim:time_asc"} <= set(shapes)
    # Every trend sorts by time; the old static examples put product_category in 3 of 5.
    assert all(e["intent"]["order_by"] == "time_asc" for e in examples if e["intent"]["time_dimension"])
    used = Counter(d for e in examples for d in e["intent"]["dimensions"])
    assert max(used.values()) <= 0.3 * sum(used.values())


class FakeExampleRetrieval(FakeRetrieval):
    def _table(self, ids):
        import pyarrow as pa

        return pa.table({"example_id": ids})


def _example_index(dense=None, lexical=None):
    table = examples_table(synthesize_examples(REGISTRY))
    return ExampleIndex(FakeExampleRetrieval(dense=dense, lexical=lexical), source="w_c_gx_1", corpus=table), table


def test_retrieve_fuses_dense_and_lexical_rankings():
    index, table = _example_index(dense={"revenue per week": ["x4", "x13", "x1"]},
                                  lexical={"revenue per week": ["x13", "x4"]})
    got = index.retrieve("revenue per week", k=2)
    questions = dict(zip(table.column("example_id").to_pylist(), table.column("question").to_pylist()))
    assert [g.question for g in got] == [questions["x4"], questions["x13"]]
    assert got[0].intent["metric"] == "total_revenue"


class _PromptLLM:
    def __init__(self):
        self.kwargs = None

    def map(self, question, registry, schema, **kwargs):
        self.kwargs = kwargs
        return SemanticIntent(metric="total_revenue")


def test_router_passes_retrieved_examples_to_the_llm_and_falls_back_to_static():
    index, _ = _example_index(dense={"revenue per week": ["x4"]})
    llm = _PromptLLM()
    router = IntentMapperRouter(config=IntentMapperConfig(mode="llm", provider="openai", model="m"),
                                llm_mapper=llm, example_index=lambda: index)
    proposal = router.propose("revenue per week", REGISTRY, _schema())
    assert [e["question"] for e in llm.kwargs["examples"]] == ["Monthly Total Revenue trend"]
    assert any("examples retrieved" in line for line in proposal.trace)

    no_index = IntentMapperRouter(config=IntentMapperConfig(mode="llm", provider="openai", model="m"),
                                  llm_mapper=llm, example_index=lambda: None)
    no_index.propose("revenue per week", REGISTRY, _schema())
    assert "examples" not in llm.kwargs

    def broken():
        raise RuntimeError("jammi down")

    IntentMapperRouter(config=IntentMapperConfig(mode="llm", provider="openai", model="m"),
                       llm_mapper=llm, example_index=broken).propose("revenue per week", REGISTRY, _schema())
    assert "examples" not in llm.kwargs


def test_prompt_uses_given_examples_instead_of_static_ones():
    given = [{"question": "Weekly Total Revenue trend", "intent": {"metric": "total_revenue"}}]
    prompt = _build_user_prompt("revenue per week", REGISTRY, _schema(), examples=given)
    assert "Weekly Total Revenue trend" in prompt
    assert "Top 10 Product Category by Total Revenue" not in prompt
    assert "Top 10 Product Category by Total Revenue" in _build_user_prompt("x", REGISTRY, _schema())


class _Store:
    def __init__(self):
        self.deleted = []

    def put(self, local, *, workspace_id, connection_id, table):
        return f"/store/{workspace_id}/{connection_id}/{table}.parquet"

    def url_for(self, *, workspace_id, connection_id, table):
        return f"/store/{workspace_id}/{connection_id}/{table}.parquet"

    def delete(self, url):
        self.deleted.append(url)


def test_builder_reuses_an_unchanged_index_and_removes_older_versions():
    client, store = FakeRetrieval(), _Store()
    first = ExampleIndexBuilder(client, store).build(workspace_id="ws", connection_id="c1", registry=REGISTRY)
    assert any(c.startswith("embed ") for c in client.calls)
    client.calls.clear()

    again = ExampleIndexBuilder(client, store).build(workspace_id="ws", connection_id="c1", registry=REGISTRY)
    assert again.source == first.source and client.calls == []

    stale = first.source[:-10] + "0123456789"
    client.sources[stale] = "old"
    remove_example_sources(client, store, "ws", "c1", keep={first.source})
    assert stale not in client.sources and first.source in client.sources
    assert store.deleted == ["/store/ws/c1/examples_0123456789.parquet"]


def test_a_repair_round_reuses_the_lookups_and_a_format_slip_is_retried_once():
    """The trace of 'albums with more than 5 tracks' showed value hints and examples searched again in
    each of three rounds for the same question, and a round spent on Instructor's 'multiple tool calls'
    (a formatting slip with nothing for a repair to correct)."""
    index, _ = _example_index(dense={"revenue per week": ["x4"]})
    calls = {"examples": 0}

    def counting_index():
        calls["examples"] += 1
        return index

    class _SlipOnce(_PromptLLM):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def map(self, question, registry, schema, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("Instructor does not support multiple tool calls, use List[Model] instead")
            return super().map(question, registry, schema, **kwargs)

    llm = _SlipOnce()
    router = IntentMapperRouter(config=IntentMapperConfig(mode="llm", provider="openai", model="m"),
                                llm_mapper=llm, example_index=counting_index)
    first = router.propose("revenue per week", REGISTRY, _schema())
    assert first.error is None and llm.attempts == 2 and any("format retry" in line for line in first.trace)
    cache = {"hints": first.value_hints, "examples": first.examples}
    again = router.propose("revenue per week", REGISTRY, _schema(), context="Your previous answer ...", cache=cache)
    assert calls["examples"] == 1 and llm.kwargs["examples"] == first.examples
    assert any("(reused)" in line for line in again.trace)
