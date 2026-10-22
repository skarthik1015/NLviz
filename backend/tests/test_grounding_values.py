"""Value grounding, offline: normalization, corpus, exact/synonym snapping, fusion and confidence,
and the index builder's reuse and cleanup (with a fake Jammi client)."""
from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from app.grounding.text import fold, trigrams
from app.grounding.values import (
    ValueIndex,
    ValueIndexBuilder,
    corpus_hash,
    corpus_table,
    remove_grounding_sources,
    snap_intent,
)
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import load_semantic_registry
from app.services.jammi_sources import source_id_for
from app.services.jammi_uploads import LocalUploadStore

REGISTRY = load_semantic_registry(Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml")
VALUES = {
    "customer_state": ["RJ", "SP", "SC"],
    "payment_type": ["boleto", "credit_card", "voucher"],
    "order_status": ["canceled", "delivered"],
}


def test_fold_and_trigrams():
    assert fold("São  Paulo-Capital!") == "sao paulo capital"
    assert fold("credit_card") == "credit card"
    assert trigrams("Rio") == "#ri rio io#"
    assert trigrams("a") == "#a#"


def test_corpus_rows_carry_folded_text_with_synonyms():
    table = corpus_table(VALUES, REGISTRY)
    rows = {r["value_id"]: r for r in table.to_pylist()}
    assert rows["customer_state:SP"]["text"] == "sp sao paulo"
    assert rows["payment_type:boleto"]["text"] == "boleto bank slip boleto bancario invoice"
    assert rows["customer_state:SP"]["tri"].startswith("#sp sp#")
    assert corpus_hash(table) == corpus_hash(corpus_table(VALUES, REGISTRY))
    assert corpus_hash(table) != corpus_hash(corpus_table({**VALUES, "order_status": ["shipped"]}, REGISTRY))


class FakeRetrieval:
    """Returns scripted rankings per (retriever, phrase)."""

    def __init__(self, dense=None, lexical=None, trigram=None):
        self.rank = {"dense": dense or {}, "lexical": lexical or {}, "trigram": trigram or {}}
        self.sources: dict[str, str] = {}
        self.calls: list[str] = []

    def encode_query(self, *, model, query):
        self._last = query
        return [0.0]

    def _table(self, ids):
        return pa.table({"value_id": ids})

    def search(self, source, *, query, k, filter=None):
        return self._table(self.rank["dense"].get(self._last, []))

    def lexical_search(self, source, *, text, k, filter=None):
        kind = "trigram" if source.endswith("_tri") or "_gt_" in source else "lexical"
        return self._table(self.rank[kind].get(self._last, []))

    def rrf_fuse(self, lists):
        scores: dict[str, float] = {}
        for ranked in lists:
            for i, item in enumerate(ranked):
                scores[item] = scores.get(item, 0) + 1 / (60 + i + 1)
        return sorted(scores.items(), key=lambda kv: -kv[1])

    # builder side
    def source_ids(self):
        return set(self.sources)

    def add_source(self, source_id, *, url, format=None):
        self.calls.append(f"add {source_id}")
        self.sources[source_id] = url

    def remove_source(self, source_id):
        self.calls.append(f"remove {source_id}")
        self.sources.pop(source_id, None)

    def generate_embeddings(self, *, source, model, columns, key):
        self.calls.append(f"embed {source}")
        return "emb"

    def build_lexical_index(self, source, *, columns, key, analyzer):
        self.calls.append(f"lex {source} {analyzer}")
        return "lex"


def _index(client) -> ValueIndex:
    return ValueIndex(client, text_source="w_c_gv_x", trigram_source="w_c_gt_x",
                      corpus=corpus_table(VALUES, REGISTRY), registry=REGISTRY)


@pytest.mark.parametrize("raw,expected", [("SP", "SP"), ("são paulo", "SP"), ("Sao Paulo", "SP"),
                                          ("bank slip", "boleto"), ("CANCELLED", "canceled")])
def test_exact_and_synonym_matches_snap_without_retrieval(raw, expected):
    dimension = {"SP": "customer_state", "são paulo": "customer_state", "Sao Paulo": "customer_state",
                 "bank slip": "payment_type", "CANCELLED": "order_status"}[raw]
    result = _index(FakeRetrieval()).snap(dimension, raw)
    assert (result.value, result.confidence) == (expected, "exact")


def test_two_retrievers_agreeing_is_confident():
    client = FakeRetrieval(dense={"cartao": ["payment_type:credit_card", "payment_type:voucher"]},
                           lexical={"cartao": ["payment_type:credit_card"]},
                           trigram={"cartao": ["payment_type:voucher"]})
    result = _index(client).snap("payment_type", "cartao")
    assert (result.value, result.confidence) == ("credit_card", "agree")


def test_disagreement_is_weak_and_offers_alternatives():
    client = FakeRetrieval(dense={"rio": ["customer_state:RJ"]}, lexical={"rio": ["customer_state:SC"]},
                           trigram={"rio": ["customer_state:SP"]})
    result = _index(client).snap("customer_state", "rio")
    assert result.value is None and result.confidence == "weak"
    assert set(result.alternatives) == {"RJ", "SC", "SP"}


def test_unknown_dimension_is_not_snapped():
    assert _index(FakeRetrieval()).snap("customer_id", "x").confidence == "none"


def test_candidates_include_exact_mentions_in_the_question():
    found = _index(FakeRetrieval()).candidates("revenue from São Paulo paid by bank slip")
    assert {(m.dimension, m.value) for m in found if m.exact} == {("customer_state", "SP"), ("payment_type", "boleto")}


def test_snapping_rewrites_only_confident_string_values():
    intent = SemanticIntent(metric="total_revenue", filters=[
        FilterCondition(dimension="customer_state", operator="eq", value="São Paulo"),
        FilterCondition(dimension="payment_type", operator="in", value=["bank slip", "voucher"]),
        FilterCondition(dimension="review_score", operator="gte", value=4),
    ])
    outcome = snap_intent(intent, _index(FakeRetrieval()))
    assert [f.value for f in outcome.intent.filters] == ["SP", ["boleto", "voucher"], 4]
    assert any("'São Paulo' to customer_state = 'SP'" in line for line in outcome.trace)


def test_builder_builds_once_then_reuses_and_cleans_old_versions(tmp_path: Path):
    client = FakeRetrieval()
    store = LocalUploadStore(tmp_path / "u", "/u")
    builder = ValueIndexBuilder(client, store)
    first = builder.build(workspace_id="u1", connection_id="c1", registry=REGISTRY, values=VALUES)
    assert sum(c.startswith("embed") for c in client.calls) == 1
    assert len(list((tmp_path / "u").rglob("values_*.parquet"))) == 1

    again = builder.build(workspace_id="u1", connection_id="c1", registry=REGISTRY, values=VALUES)
    assert again.sources == first.sources and sum(c.startswith("embed") for c in client.calls) == 1

    changed = builder.build(workspace_id="u1", connection_id="c1", registry=REGISTRY,
                            values={**VALUES, "order_status": ["shipped"]})
    assert set(client.sources) == set(changed.sources)  # the old version's sources were removed
    assert len(list((tmp_path / "u").rglob("values_*.parquet"))) == 1  # and its corpus file

    remove_grounding_sources(client, store, "u1", "c1")
    assert client.sources == {} and not list((tmp_path / "u").rglob("*.parquet"))


def test_cleanup_leaves_other_sources_of_the_connection_alone(tmp_path: Path):
    client = FakeRetrieval()
    keep = source_id_for("u1", "c1", "pg")
    client.sources = {keep: "postgresql://x", source_id_for("u1", "c1", "gv_abc"): "/u/x"}
    remove_grounding_sources(client, None, "u1", "c1")
    assert set(client.sources) == {keep}


def test_extraction_writes_values_as_filters_spell_them():
    from app.grounding.values import extract_values

    rows = [{"review_score": 5.0}, {"review_score": float("nan")}, {"review_score": None}, {"review_score": 4.0}]
    values = extract_values(REGISTRY, run_query=lambda sql: rows, build_sql=lambda intent, reg: "SQL")
    assert values["review_score"] == {"4": [], "5": []}


def test_extraction_uses_a_metric_without_a_base_filter():
    from app.grounding.values import extract_values

    seen = []
    extract_values(REGISTRY, run_query=lambda sql: [], build_sql=lambda intent, reg: seen.append(intent.metric) or "")
    assert set(seen) == {"order_count"}  # revenue's filter would hide cancelled orders


# --- the intent router uses grounding ---


class _FakeLLM:
    def __init__(self, intent):
        self.intent, self.hints = intent, "not called"

    def map(self, question, registry, schema, value_hints=None):
        self.hints = value_hints
        return self.intent


def _router(index_fn, llm):
    from app.services.intent_mapper import IntentMapperConfig, IntentMapperRouter

    config = IntentMapperConfig(mode="llm", fallback_to_heuristic=False)
    return IntentMapperRouter(config=config, llm_mapper=llm, value_index=index_fn)


def _schema():
    from app.connectors.base import SchemaContext

    tables = {t.name: [] for t in REGISTRY.schema.tables}
    return SchemaContext(tables=tables, row_counts={}, join_paths=[])


def test_router_passes_value_hints_to_the_llm():
    """Snapping the LLM's filters happens later, in check_intent (snap_intent)."""
    intent = SemanticIntent(metric="total_revenue", filters=[
        FilterCondition(dimension="customer_state", operator="eq", value="São Paulo")])
    llm = _FakeLLM(intent)
    result = _router(lambda: _index(FakeRetrieval()), llm).propose("revenue from São Paulo", REGISTRY, _schema())
    assert {"dimension": "customer_state", "value": "SP", "exact": True} in llm.hints
    assert result.value_hints == llm.hints and result.intent.filters[0].value == "São Paulo"


def test_router_without_an_index_behaves_as_before():
    intent = SemanticIntent(metric="total_revenue", filters=[
        FilterCondition(dimension="customer_state", operator="eq", value="São Paulo")])
    llm = _FakeLLM(intent)
    result = _router(lambda: None, llm).propose("revenue from São Paulo", REGISTRY, _schema())
    assert llm.hints is None and result.intent.filters[0].value == "São Paulo"


def test_router_survives_a_failing_index():
    class Broken:
        dimensions = ["customer_state"]

        def candidates(self, question):
            raise RuntimeError("jammi down")

        def snap(self, dimension, raw):
            raise RuntimeError("jammi down")

    intent = SemanticIntent(metric="total_revenue", filters=[
        FilterCondition(dimension="customer_state", operator="eq", value="SP")])
    result = _router(lambda: Broken(), _FakeLLM(intent)).propose("revenue in SP", REGISTRY, _schema())
    assert result.intent.filters[0].value == "SP" and result.source == "llm" and result.error is None


def test_candidates_add_a_retrieved_value_only_when_two_retrievers_rank_it_first():
    health = "product_category:health_beauty"
    agreed = FakeRetrieval(dense={"health and beauty": [health, "product_category:pet_shop"]},
                           lexical={"health and beauty": [health]})
    found = _index(agreed).candidates("health and beauty")
    assert [(m.dimension, m.value, m.exact) for m in found] == [("product_category", "health_beauty", False)]

    # A question that names no value: only the dense retriever returns anything (its nearest
    # neighbours), the trigram retriever disagrees -> no hints at all.
    noise = FakeRetrieval(dense={"weekly revenue trend": ["payment_type:voucher", health]},
                          trigram={"weekly revenue trend": ["order_status:delivered"]})
    assert _index(noise).candidates("weekly revenue trend") == []


def test_assumption_accepts_values_the_question_supports_and_offers_the_rest():
    # Named in the question (value or alias): supported without retrieval.
    plain = _index(FakeRetrieval())
    assert plain.assumption("revenue from São Paulo", "customer_state", "SP") is None
    assert plain.assumption("orders paid by bank slip", "payment_type", "boleto") is None

    rs, rn = "customer_state:RS", "customer_state:RN"
    question = "revenue in rio grande"
    # Two retrievers rank the chosen value first among the dimension's values: supported.
    agreed = FakeRetrieval(dense={question: [rs, rn]}, lexical={question: [rs, rn]})
    assert _index(agreed).assumption(question, "customer_state", "RS") is None
    # They disagree (or rank another value first): the chosen value first, then the others.
    split = FakeRetrieval(dense={question: [rs, rn]}, lexical={question: [rn, rs]})
    assert _index(split).assumption(question, "customer_state", "RS") == ["RS", "RN"]
    assert _index(agreed).assumption(question, "customer_state", "RN") == ["RN", "RS"]
