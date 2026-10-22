"""Value grounding: find the real values behind words in a question ("São Paulo" → SP).

Corpus: the distinct values of each low/medium-cardinality dimension, read through the connection's
own compiler and connector (so joins and base filters match what queries see), plus synonyms
declared in the semantic YAML. It is written to managed storage and registered on jammi-server as
two sources over the same Parquet file:
- `<...>_gv_<hash>`: dense embeddings (multilingual MiniLM) + BM25 over accent-folded text;
- `<...>_gt_<hash>`: BM25 over character trigrams (typos).
Sources are named by a hash of the corpus, so an unchanged schema reuses its indexes across restarts.

Retrieval (Phase 0: 0.95 top-1 on 56 cases): exact match on the folded value or a synonym first;
otherwise reciprocal-rank fusion of the three retrievers. A match is confident when it is exact or
when at least two of the three retrievers rank it first.
"""
from __future__ import annotations

import hashlib
import logging
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from app.grounding.text import fold, trigrams
from app.semantic import SemanticRegistry
from app.services.jammi_sources import source_id_for

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MAX_VALUES_PER_DIMENSION = 5000
# Meaning matches for list items (ValueIndex.similar); tuned on the restaurant file's cuisines.
SIMILAR_K = 15
SIMILAR_MIN = 0.5
SIMILAR_MARGIN = 0.15
_SNAP_OPERATORS = {"eq", "ne", "in", "not_in"}
# On a list dimension "contains" is snapped too: ILIKE '%Asian%' also matched "Pan Asian".
_LIST_OPERATORS = {"eq", "in", "contains"}


def _snap_list_filter(condition, index: "ValueIndex", trace: list[str], evidence: list[dict]):
    """A filter on a list dimension, item by item: a word that names an item (folded, or a
    synonym) keeps it; any other word becomes the items closest in meaning ("sweets" -> Desserts,
    Mithai, Bakery). The result is eq for one item, else in (any of them). With no match at all the
    filter is left as written."""
    raw_values = condition.value if isinstance(condition.value, list) else [condition.value]
    items: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            return condition
        exact = index.exact(condition.dimension, raw)
        if exact is not None:
            matches = [exact]
            evidence.append({"dimension": condition.dimension, "text": raw, "value": exact, "confidence": "exact",
                             "alternatives": []})
        else:
            scored = index.similar(condition.dimension, raw)
            matches = [value for value, _ in scored]
            evidence.append({"dimension": condition.dimension, "text": raw, "value": ", ".join(matches) or None,
                             "confidence": "meaning" if matches else "none", "alternatives": [],
                             "scores": {value: round(score, 3) for value, score in scored}})
            trace.append(f"No {condition.dimension} item is called '{raw}'; "
                         + (f"matched by meaning: {', '.join(matches)}" if matches else "nothing close in meaning"))
        items += [m for m in matches if m not in items]
    if not items:
        return condition
    if len(items) == 1:
        return condition.model_copy(update={"operator": "eq", "value": items[0]})
    return condition.model_copy(update={"operator": "in", "value": items})


class GroundingClient(Protocol):
    def add_source(self, source_id: str, *, url: str, format: str | None = None) -> None: ...
    def remove_source(self, source_id: str) -> None: ...
    def source_ids(self) -> set[str]: ...
    def generate_embeddings(self, *, source: str, model: str, columns: list[str], key: str) -> str: ...
    def build_lexical_index(self, source: str, *, columns: list[str], key: str, analyzer: str) -> str: ...
    def encode_query(self, *, model: str, query: str) -> list[float]: ...
    def search(self, source: str, *, query: list[float], k: int, filter: str | None = None) -> pa.Table: ...
    def lexical_search(self, source: str, *, text: str, k: int, filter: str | None = None) -> pa.Table: ...
    def rrf_fuse(self, ranked_lists: list[list[str]]) -> list[tuple[str, float]]: ...


@dataclass(frozen=True)
class ValueMatch:
    dimension: str
    value: str
    score: float
    exact: bool = False


@dataclass(frozen=True)
class SnapResult:
    """`value` is set when the match is confident; otherwise `alternatives` lists candidates."""

    value: str | None
    confidence: str  # "exact" | "agree" | "weak" | "none"
    alternatives: list[str] = field(default_factory=list)


def _value_id(dimension: str, value: str) -> str:
    return f"{dimension}:{value}"


# A dimension's values, each with aliases taken from the data (see `aliases_from`); a plain list
# means no data aliases.
DimensionValues = dict[str, list[str]] | list[str]


def _canonical(value) -> str | None:
    """How a value is written in a filter. Query results arrive through pandas, which turns an
    integer column with NULLs into floats (5 -> 5.0, NULL -> NaN)."""
    if value is None or value == "":
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
    return str(value)


def _extraction_metric(registry: SemanticRegistry) -> str:
    """A metric whose base filter does not hide values (e.g. revenue excludes cancelled orders,
    which would drop 'canceled' from the order status corpus)."""
    for metric in registry.schema.metrics:
        if not metric.base_filter and metric.aggregation != "RATIO":
            return metric.name
    return registry.schema.metrics[0].name


def extract_values(
    registry: SemanticRegistry,
    run_query: Callable[[str], list[dict]],
    build_sql: Callable[[object, SemanticRegistry], str],
    *,
    max_values: int = MAX_VALUES_PER_DIMENSION,
) -> dict[str, dict[str, list[str]]]:
    """Distinct values of every low/medium-cardinality dimension, through the normal compiler.

    A dimension with an `alias_expression` also collects that expression's value for each of its
    values (e.g. the Portuguese category name next to the English one), through a temporary
    dimension that exists only for this query."""
    from app.models import SemanticIntent

    if not registry.schema.metrics:
        return {}
    metric = _extraction_metric(registry)
    values: dict[str, dict[str, list[str]]] = {}
    for dimension in registry.schema.dimensions:
        if dimension.cardinality == "high":
            continue
        alias_dimension = f"{dimension.name}__alias" if dimension.alias_expression else None
        query_registry = registry
        if alias_dimension:
            alias = dimension.model_copy(update={"name": alias_dimension, "sql_expression": dimension.alias_expression,
                                                 "alias_expression": None, "synonyms": None})
            query_registry = SemanticRegistry(registry.schema.model_copy(
                update={"dimensions": [*registry.schema.dimensions, alias]}))
        dimensions = [dimension.name] + ([alias_dimension] if alias_dimension else [])
        intent = SemanticIntent(metric=metric, dimensions=dimensions, limit=max_values)
        try:
            rows = run_query(build_sql(intent, query_registry))
        except Exception as exc:  # one broken dimension must not block the others
            logger.warning("Value extraction failed for dimension %s: %s", dimension.name, str(exc)[:200])
            continue
        found: dict[str, list[str]] = {}
        for row in rows:
            value = _canonical(row.get(dimension.name))
            if value is None:
                continue
            aliases = found.setdefault(value, [])
            alias = _canonical(row.get(alias_dimension)) if alias_dimension else None
            if alias is not None and alias != value and alias not in aliases:
                aliases.append(alias)
        if found:
            values[dimension.name] = dict(sorted(found.items()))
    return values


def corpus_table(values: dict[str, DimensionValues], registry: SemanticRegistry) -> pa.Table:
    """One row per value. `aliases` holds every other way to say it ("|"-separated): synonyms from
    the YAML plus aliases taken from the data."""
    rows: dict[str, list[str]] = {"value_id": [], "dimension": [], "value": [], "aliases": [], "text": [], "tri": []}
    for dimension_name, dimension_values in sorted(values.items()):
        synonyms = registry.get_dimension(dimension_name).synonyms or {}
        if isinstance(dimension_values, list):
            dimension_values = {value: [] for value in dimension_values}
        for value, data_aliases in dimension_values.items():
            aliases = list(dict.fromkeys([*synonyms.get(value, []), *data_aliases]))
            spoken = " ".join(aliases)
            rows["value_id"].append(_value_id(dimension_name, value))
            rows["dimension"].append(dimension_name)
            rows["value"].append(value)
            rows["aliases"].append("|".join(aliases))
            rows["text"].append(fold(f"{value} {spoken}"))
            rows["tri"].append(trigrams(f"{value} {spoken}"))
    return pa.table(rows)


def corpus_hash(table: pa.Table) -> str:
    digest = hashlib.sha256()
    for column in ("value_id", "text"):
        for item in table.column(column).to_pylist():
            digest.update(item.encode("utf-8"))
            digest.update(b"\0")
    digest.update(EMBEDDING_MODEL.encode())
    return digest.hexdigest()[:10]


_TRUE_WORDS = {"yes", "true", "y", "1", "t"}
_FALSE_WORDS = {"no", "false", "n", "0", "f"}


def _plural_forms(alias: str) -> tuple[str, ...]:
    """A value as the question may say it: "cafe" -> cafe, cafes; "bakery" -> bakery, bakeries.
    Only for words of 3+ letters: "sp" must not match "sps"."""
    if len(alias) < 3 or not alias[-1].isalpha():
        return (alias,)
    forms = [alias, alias + "s", alias + "es"]
    if alias.endswith("y"):
        forms.append(alias[:-1] + "ies")
    return tuple(forms)


def _singular_forms(word: str) -> tuple[str, ...]:
    """The reverse of _plural_forms, the word itself first."""
    forms = [word]
    if len(word) > 4 and word.endswith("ies"):
        forms.append(word[:-3] + "y")
    if len(word) > 3 and word.endswith("es"):
        forms.append(word[:-2])
    if len(word) > 3 and word.endswith("s"):
        forms.append(word[:-1])
    return tuple(forms)


class ValueIndex:
    def __init__(
        self,
        client: GroundingClient,
        *,
        text_source: str,
        trigram_source: str,
        corpus: pa.Table,
        registry: SemanticRegistry,
        model: str = EMBEDDING_MODEL,
    ):
        self._client = client
        self._text_source = text_source
        self._trigram_source = trigram_source
        self._model = model
        self.dimensions = sorted(set(corpus.column("dimension").to_pylist()))
        # Dimensions whose values are list items (cuisines): a word naming no item is matched by meaning.
        self.list_dimensions = {d.name for d in registry.schema.dimensions if d.separator} & set(self.dimensions)
        # Words of each dimension's own name and label ("artist", "name" for Artist Name).
        self._label_words = {d.name: {form for word in fold(f"{d.name.replace('_', ' ')} {d.display_name}").split()
                                      for form in (*_singular_forms(word), word)}
                             for d in registry.schema.dimensions}
        # Exact lookup: folded value or synonym → canonical value, per dimension.
        self._exact: dict[tuple[str, str], str] = {}
        for dimension, value, aliases in zip(corpus.column("dimension").to_pylist(),
                                             corpus.column("value").to_pylist(),
                                             corpus.column("aliases").to_pylist()):
            self._exact.setdefault((dimension, fold(value)), value)
            for alias in filter(None, aliases.split("|")):
                self._exact.setdefault((dimension, fold(alias)), value)

    @property
    def sources(self) -> tuple[str, str]:
        return self._text_source, self._trigram_source

    def _ranked(self, phrase: str, k: int, sql_filter: str | None) -> tuple[list[str], list[str], list[str]]:
        query = self._client.encode_query(model=self._model, query=phrase)
        ids = lambda table: table.column("value_id").to_pylist() if table.num_rows else []  # noqa: E731
        dense = ids(self._client.search(self._text_source, query=query, k=k, filter=sql_filter))
        lexical = ids(self._client.lexical_search(self._text_source, text=fold(phrase), k=k, filter=sql_filter))
        trigram = ids(self._client.lexical_search(self._trigram_source, text=trigrams(phrase), k=k, filter=sql_filter))
        return dense, lexical, trigram

    def snap(self, dimension: str, raw_value: str, *, k: int = 10) -> SnapResult:
        """Map a value the LLM wrote to a real value of the dimension."""
        if dimension not in self.dimensions:
            return SnapResult(value=None, confidence="none")
        exact = self.exact(dimension, raw_value)
        if exact is not None:
            return SnapResult(value=exact, confidence="exact")
        ranked = self._ranked(str(raw_value), k, f"dimension = '{dimension}'")
        fused = [value_id for value_id, _ in self._client.rrf_fuse([r for r in ranked if r])]
        if not fused:
            return SnapResult(value=None, confidence="none")
        values = [value_id.split(":", 1)[1] for value_id in fused]
        agree = sum(1 for r in ranked if r and r[0] == fused[0])
        if agree >= 2:
            return SnapResult(value=values[0], confidence="agree", alternatives=values[1:4])
        return SnapResult(value=None, confidence="weak", alternatives=values[:4])

    def values(self, dimension: str) -> set[str]:
        """The indexed values of a dimension (canonical spelling)."""
        return {value for (d, _), value in self._exact.items() if d == dimension}

    def exact(self, dimension: str, raw_value: str) -> str | None:
        """The value named word for word, also in the plural or singular ("cafes" -> Cafe), and the
        yes/no of a yes/no dimension however it is written (the LLM filtered is_delivering_now =
        'true' on a column of Yes/No and found nothing)."""
        folded = fold(str(raw_value))
        for form in dict.fromkeys(_singular_forms(folded) + _plural_forms(folded)):  # "juice" -> Juices
            value = self._exact.get((dimension, form))
            if value is not None:
                return value
        values = self.values(dimension)
        if self.is_yes_no(dimension):
            for truths in (_TRUE_WORDS, _FALSE_WORDS):
                if folded in truths:
                    return next((v for v in values if fold(v) in truths), None)
        return None

    def is_yes_no(self, dimension: str) -> bool:
        """Two values, one yes and one no (not price range 1-4)."""
        values = {fold(v) for v in self.values(dimension)}
        return len(values) == 2 and bool(values & _TRUE_WORDS) and bool(values & _FALSE_WORDS)

    def similar(self, dimension: str, phrase: str, *, k: int = SIMILAR_K) -> list[tuple[str, float]]:
        """Values of a dimension close in meaning to a phrase that names none of them ("sweets" ->
        Desserts, Mithai, Bakery), best first: dense similarity at least SIMILAR_MIN and within
        SIMILAR_MARGIN of the best. The corpus is the value index built at publish, so this costs
        one query embedding and one search; nothing new is embedded."""
        query = self._client.encode_query(model=self._model, query=phrase)
        table = self._client.search(self._text_source, query=query, k=k, filter=f"dimension = '{dimension}'")
        if not table.num_rows or "similarity" not in table.column_names:
            return []
        scored = sorted(zip(table.column("value").to_pylist(), table.column("similarity").to_pylist()),
                        key=lambda item: -item[1])
        best = scored[0][1]
        return [(value, score) for value, score in scored
                if score >= SIMILAR_MIN and score >= best - SIMILAR_MARGIN]

    def assumption(self, question: str, dimension: str, value: str, *, k: int = 10) -> list[str] | None:
        """Whether the question supports a value the LLM chose. None when it does: the question
        names the value or one of its aliases, or two retrievers searching this dimension with the
        question rank the value first. Otherwise the close candidates, the chosen value first, when
        there is more than one: the LLM picked one of several ('Rio Grande' -> RS or RN?)."""
        folded = f" {fold(question)} "
        for (alias_dimension, alias), aliased in self._exact.items():
            if alias_dimension == dimension and aliased == value and len(alias) >= 2 and f" {alias} " in folded:
                return None
        ranked = self._ranked(question, k, f"dimension = '{dimension}'")
        fused = [value_id for value_id, _ in self._client.rrf_fuse([r for r in ranked if r])]
        if fused and fused[0] == _value_id(dimension, value) and sum(1 for r in ranked if r and r[0] == fused[0]) >= 2:
            return None
        others = [value_id.split(":", 1)[1] for value_id in fused if value_id != _value_id(dimension, value)]
        options = [value, *others[:3]]
        return options if len(options) >= 2 else None

    def _only_label_words(self, question: str, dimension: str, value: str) -> bool:
        """Whether the value shares words with the question only through the dimension's own name:
        "Total Invoice Amount by Artist Name" retrieved "Various Artists" on the word "artist"."""
        def forms(text: str) -> set[str]:
            return {form for word in fold(text).split() for form in (*_singular_forms(word), word)}

        shared = forms(question) & forms(value)
        return bool(shared) and shared <= self._label_words.get(dimension, set())

    def candidates(self, question: str, *, k: int = 8) -> list[ValueMatch]:
        """Values a question mentions, for the LLM prompt: exact or alias mentions, plus the best
        retrieved value when at least two retrievers rank it first (the same rule that makes a snap
        confident). A question that names no value gets no hints: the nearest values of an
        unrelated question are noise that pulls the LLM towards wrong filters and dimensions
        (Phase 3: 'orders by month' became 'by product category')."""
        found: list[ValueMatch] = []
        folded = f" {fold(question)} "
        for (dimension, alias), value in self._exact.items():
            if len(alias) >= 2 and any(f" {form} " in folded for form in _plural_forms(alias)):
                found.append(ValueMatch(dimension, value, 1.0, exact=True))
        ranked = self._ranked(question, 20, None)
        fused = self._client.rrf_fuse([r for r in ranked if r])
        if fused:
            value_id, score = fused[0]
            dimension, value = value_id.split(":", 1)
            agree = sum(1 for r in ranked if r and r[0] == value_id)
            if (agree >= 2 and not any(m.dimension == dimension and m.value == value for m in found)
                    and not self._only_label_words(question, dimension, value)):
                found.append(ValueMatch(dimension, value, score))
        return found[:k]


@dataclass(frozen=True)
class UnresolvedValue:
    """A filter value to ask the user about. reason "no_match": the LLM's value matches nothing
    confidently but has close candidates; "assumed": the LLM chose a real value the question does
    not support, among several close ones."""
    filter_index: int
    dimension: str
    raw: str
    options: list[str]
    reason: str = "no_match"


@dataclass(frozen=True)
class SnapOutcome:
    intent: object
    trace: list[str]
    unresolved: list[UnresolvedValue]
    # One entry per value looked up, for the evidence panel.
    evidence: list[dict]


def snap_intent(
    intent, index: ValueIndex, *, question: str | None = None, confirmed: set[tuple[str, str]] = frozenset()
) -> SnapOutcome:
    """Replace filter values with real values where the match is confident (values that cannot be
    snapped confidently are left as written), and report the values the user should be asked about. With the question,
    a confidently snapped value is also checked against it (`ValueIndex.assumption`), except values
    in `confirmed` (chosen by the user, or carried from the previous turn)."""
    trace: list[str] = []
    unresolved: list[UnresolvedValue] = []
    evidence: list[dict] = []
    new_filters = []
    for position, condition in enumerate(intent.filters):
        if condition.dimension in getattr(index, "list_dimensions", ()) and condition.operator in _LIST_OPERATORS:
            new_filters.append(_snap_list_filter(condition, index, trace, evidence))
            continue
        if condition.operator not in _SNAP_OPERATORS or condition.dimension not in index.dimensions:
            new_filters.append(condition)
            continue
        raw_values = condition.value if isinstance(condition.value, list) else [condition.value]
        snapped_values = []
        for raw in raw_values:
            if not isinstance(raw, str):
                snapped_values.append(raw)
                continue
            result = index.snap(condition.dimension, raw)
            evidence.append({"dimension": condition.dimension, "text": raw, "value": result.value,
                             "confidence": result.confidence, "alternatives": list(result.alternatives)})
            if result.value is not None and result.value != raw:
                trace.append(f"Matched '{raw}' to {condition.dimension} = '{result.value}' ({result.confidence})")
            elif result.value is None and result.alternatives:
                trace.append(f"'{raw}' is not a known {condition.dimension}; closest: {', '.join(result.alternatives)}")
                unresolved.append(UnresolvedValue(position, condition.dimension, raw, list(result.alternatives)))
            # A question never names Yes or No ("don't have online delivery"); the wording decides it.
            if (result.value is not None and question and (condition.dimension, result.value) not in confirmed
                    and not getattr(index, "is_yes_no", lambda _: False)(condition.dimension)):
                options = index.assumption(question, condition.dimension, result.value)
                if options:
                    trace.append(f"'{result.value}' is not named in the question; close: {', '.join(options[1:])}")
                    evidence[-1]["assumed"] = True
                    unresolved.append(UnresolvedValue(position, condition.dimension, result.value, options, "assumed"))
            snapped_values.append(result.value if result.value is not None else raw)
        value = snapped_values if isinstance(condition.value, list) else snapped_values[0]
        new_filters.append(condition.model_copy(update={"value": value}))
    return SnapOutcome(intent.model_copy(update={"filters": new_filters}), trace, unresolved, evidence)


def _grounding_prefixes(workspace_id: str, connection_id: str) -> tuple[str, str]:
    base = source_id_for(workspace_id, connection_id, "x")[:-1]  # "w<tag>_c<conn>_"
    return f"{base}gv_", f"{base}gt_"


def remove_grounding_sources(client, store, workspace_id: str, connection_id: str, *, keep: set[str] = frozenset()) -> None:
    """Remove a connection's value-index sources (all, or all but `keep`) and their corpus files."""
    prefixes = _grounding_prefixes(workspace_id, connection_id)
    for source in sorted(client.source_ids()):
        if source in keep or not source.startswith(prefixes):
            continue
        try:
            client.remove_source(source)
        except Exception:
            logger.warning("Could not remove grounding source %s", source, exc_info=True)
        if source.startswith(prefixes[0]) and store is not None:
            digest = source[len(prefixes[0]):]
            try:
                store.delete(store.url_for(workspace_id=workspace_id, connection_id=connection_id,
                                           table=f"values_{digest}"))
            except Exception:
                logger.warning("Could not delete grounding corpus %s", digest, exc_info=True)


class ValueIndexBuilder:
    """Builds (or reuses) a connection's value index on jammi-server."""

    def __init__(self, client: GroundingClient, store, *, model: str = EMBEDDING_MODEL):
        self._client = client
        self._store = store  # ManagedUploadStore: where jammi-server can read the corpus file
        self._model = model

    def build(
        self, *, workspace_id: str, connection_id: str, registry: SemanticRegistry, values: dict[str, DimensionValues]
    ) -> ValueIndex | None:
        table = corpus_table(values, registry)
        if table.num_rows == 0:
            return None
        digest = corpus_hash(table)
        text_source = source_id_for(workspace_id, connection_id, f"gv_{digest}")
        trigram_source = source_id_for(workspace_id, connection_id, f"gt_{digest}")
        existing = self._client.source_ids()
        if not {text_source, trigram_source} <= existing:
            for stale in (text_source, trigram_source):
                if stale in existing:  # half-built earlier: start clean
                    self._client.remove_source(stale)
            with tempfile.TemporaryDirectory() as workdir:
                local = Path(workdir) / f"values_{digest}.parquet"
                pq.write_table(table, local)
                url = self._store.put(local, workspace_id=workspace_id, connection_id=connection_id,
                                      table=f"values_{digest}")
            self._client.add_source(text_source, url=url, format="parquet")
            self._client.add_source(trigram_source, url=url, format="parquet")
            try:
                self._client.generate_embeddings(source=text_source, model=self._model, columns=["text"], key="value_id")
                self._client.build_lexical_index(text_source, columns=["text"], key="value_id", analyzer="english")
                self._client.build_lexical_index(trigram_source, columns=["tri"], key="value_id", analyzer="raw")
            except Exception:
                for source in (text_source, trigram_source):
                    self._client.remove_source(source)
                raise
            logger.info("Built value index %s (%d values) for %s", digest, table.num_rows, connection_id)
            remove_grounding_sources(self._client, self._store, workspace_id, connection_id,
                                     keep={text_source, trigram_source})
        return ValueIndex(self._client, text_source=text_source, trigram_source=trigram_source,
                          corpus=table, registry=registry, model=self._model)
