"""Examples index: few-shot examples retrieved per question (Phase 3d).

The LLM used to see the same five examples for every question, all built on the first metric and
the first dimension; it copied that dimension into unrelated questions ("orders by month" →
by product category). Instead, a corpus of examples is synthesized from the connection's own
registry, so it exists for every schema (hand-written or generated):
- every metric appears in every question shape (total, breakdown, top N, lowest, trend, trend with
  a breakdown, date range, secondary time dimension);
- dimensions and granularities rotate, so no single dimension dominates.

The corpus is registered on jammi-server as one source `<...>_gx_<hash>` (dense multilingual
MiniLM + English BM25 over the example question); a question gets the examples nearest to it by
reciprocal-rank fusion of the two. An unchanged registry reuses its source across restarts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.grounding.values import EMBEDDING_MODEL, GroundingClient
from app.semantic import SemanticRegistry
from app.services.jammi_sources import source_id_for

logger = logging.getLogger(__name__)

EXAMPLES_PER_QUESTION = 4
_ADJECTIVE = {"day": "Daily", "week": "Weekly", "month": "Monthly", "quarter": "Quarterly", "year": "Yearly"}
_GRANULARITIES = ("month", "week", "quarter", "year", "day")


def _intent(metric: str, *, dimensions=(), time_dimension=None, granularity=None, order_by="metric_desc",
            limit=100, start=None, end=None) -> dict:
    return {"metric": metric, "dimensions": list(dimensions), "filters": [], "time_dimension": time_dimension,
            "time_granularity": granularity, "start_date": start, "end_date": end, "order_by": order_by,
            "limit": limit}


def synthesize_examples(registry: SemanticRegistry) -> list[dict]:
    """(question, intent) pairs covering every metric in every question shape.

    No value-filter examples: measured in Phase 3d, the 4B model copied their "by X for value"
    shape into filter-only questions (value questions 19/20 -> 16/20)."""
    schema = registry.schema
    groupable = [d for d in schema.dimensions if d.cardinality != "high"] or list(schema.dimensions)
    ranked = list(schema.dimensions)  # "top N <dimension>" also makes sense for high cardinality (customers)
    time_dims = list(schema.time_dimensions)
    examples: list[dict] = []
    for i, metric in enumerate(schema.metrics):
        m = metric.display_name
        dim = groupable[i % len(groupable)] if groupable else None
        other = groupable[(i + 1) % len(groupable)] if groupable else None
        top = ranked[(i + 2) % len(ranked)] if ranked else None
        gran = _GRANULARITIES[i % len(_GRANULARITIES)]
        gran2 = _GRANULARITIES[(i + 2) % len(_GRANULARITIES)]

        examples.append({"question": f"What is the overall {m}?", "intent": _intent(metric.name)})
        if dim:
            examples.append({"question": f"{m} by {dim.display_name}",
                             "intent": _intent(metric.name, dimensions=[dim.name])})
        if top:
            examples.append({"question": f"Top 5 {top.display_name} by {m}",
                             "intent": _intent(metric.name, dimensions=[top.name], limit=5)})
        if other:
            examples.append({"question": f"Which {other.display_name} has the lowest {m}?",
                             "intent": _intent(metric.name, dimensions=[other.name], order_by="metric_asc")})
        if time_dims:
            td = time_dims[0]
            examples.append({"question": f"{_ADJECTIVE[gran]} {m} trend",
                             "intent": _intent(metric.name, time_dimension=td.name, granularity=gran,
                                               order_by="time_asc")})
            if other:
                examples.append({"question": f"{_ADJECTIVE[gran2]} {m} by {other.display_name} over time",
                                 "intent": _intent(metric.name, dimensions=[other.name], time_dimension=td.name,
                                                   granularity=gran2, order_by="time_asc")})
            for td2 in time_dims[1:]:
                examples.append({"question": f"{_ADJECTIVE['month']} {m} by {td2.display_name}",
                                 "intent": _intent(metric.name, time_dimension=td2.name, granularity="month",
                                                   order_by="time_asc")})
        if dim:
            examples.append({"question": f"{m} by {dim.display_name} in 2018",
                             "intent": _intent(metric.name, dimensions=[dim.name], start="2018-01-01",
                                               end="2018-12-31")})
        examples.append({"question": f"{m} from March to May 2017",
                         "intent": _intent(metric.name, start="2017-03-01", end="2017-05-31")})
    return examples


def examples_table(examples: list[dict]) -> pa.Table:
    return pa.table({
        "example_id": [f"x{i}" for i in range(len(examples))],
        "question": [e["question"] for e in examples],
        "intent": [json.dumps(e["intent"], separators=(",", ":"), sort_keys=True) for e in examples],
    })


def examples_hash(table: pa.Table) -> str:
    digest = hashlib.sha256()
    for column in ("example_id", "question", "intent"):
        for item in table.column(column).to_pylist():
            digest.update(item.encode("utf-8"))
            digest.update(b"\0")
    digest.update(EMBEDDING_MODEL.encode())
    return digest.hexdigest()[:10]


def _in_field_order(intent: dict) -> dict:
    """The intent's keys in SemanticIntent's own order. The corpus stores them sorted (for a stable
    hash), and shown that way the 4B model answered time trends with order_by metric_desc: sorted,
    order_by comes before time_dimension (run 14, held-out v2 22 -> 14). In field order: time_asc."""
    from app.models import SemanticIntent

    ordered = {name: intent[name] for name in SemanticIntent.model_fields if name in intent}
    return {**ordered, **{k: v for k, v in intent.items() if k not in ordered}}


@dataclass(frozen=True)
class RetrievedExample:
    question: str
    intent: dict


class ExampleIndex:
    def __init__(self, client: GroundingClient, *, source: str, corpus: pa.Table, model: str = EMBEDDING_MODEL):
        self._client = client
        self._source = source
        self._model = model
        self._by_id = {
            example_id: RetrievedExample(question, _in_field_order(json.loads(intent)))
            for example_id, question, intent in zip(corpus.column("example_id").to_pylist(),
                                                    corpus.column("question").to_pylist(),
                                                    corpus.column("intent").to_pylist())
        }

    @property
    def source(self) -> str:
        return self._source

    def retrieve(self, question: str, *, k: int = EXAMPLES_PER_QUESTION) -> list[RetrievedExample]:
        ids = lambda table: table.column("example_id").to_pylist() if table.num_rows else []  # noqa: E731
        query = self._client.encode_query(model=self._model, query=question)
        dense = ids(self._client.search(self._source, query=query, k=k * 3))
        lexical = ids(self._client.lexical_search(self._source, text=question, k=k * 3))
        fused = self._client.rrf_fuse([r for r in (dense, lexical) if r])
        return [self._by_id[example_id] for example_id, _ in fused[:k] if example_id in self._by_id]


def _examples_prefix(workspace_id: str, connection_id: str) -> str:
    return source_id_for(workspace_id, connection_id, "x")[:-1] + "gx_"


def remove_example_sources(client, store, workspace_id: str, connection_id: str, *, keep: set[str] = frozenset()) -> None:
    """Remove a connection's examples-index sources (all, or all but `keep`) and their corpus files."""
    prefix = _examples_prefix(workspace_id, connection_id)
    for source in sorted(client.source_ids()):
        if source in keep or not source.startswith(prefix):
            continue
        try:
            client.remove_source(source)
        except Exception:
            logger.warning("Could not remove examples source %s", source, exc_info=True)
        if store is not None:
            try:
                store.delete(store.url_for(workspace_id=workspace_id, connection_id=connection_id,
                                           table=f"examples_{source[len(prefix):]}"))
            except Exception:
                logger.warning("Could not delete examples corpus %s", source, exc_info=True)


class ExampleIndexBuilder:
    """Builds (or reuses) a connection's examples index on jammi-server."""

    def __init__(self, client: GroundingClient, store, *, model: str = EMBEDDING_MODEL):
        self._client = client
        self._store = store
        self._model = model

    def build(self, *, workspace_id: str, connection_id: str, registry: SemanticRegistry) -> ExampleIndex | None:
        examples = synthesize_examples(registry)
        if not examples:
            return None
        table = examples_table(examples)
        digest = examples_hash(table)
        source = source_id_for(workspace_id, connection_id, f"gx_{digest}")
        if source not in self._client.source_ids():
            with tempfile.TemporaryDirectory() as workdir:
                local = Path(workdir) / f"examples_{digest}.parquet"
                pq.write_table(table, local)
                url = self._store.put(local, workspace_id=workspace_id, connection_id=connection_id,
                                      table=f"examples_{digest}")
            self._client.add_source(source, url=url, format="parquet")
            try:
                self._client.generate_embeddings(source=source, model=self._model, columns=["question"],
                                                 key="example_id")
                self._client.build_lexical_index(source, columns=["question"], key="example_id", analyzer="english")
            except Exception:
                self._client.remove_source(source)
                raise
            logger.info("Built examples index %s (%d examples) for %s", digest, table.num_rows, connection_id)
            remove_example_sources(self._client, self._store, workspace_id, connection_id, keep={source})
        return ExampleIndex(self._client, source=source, corpus=table, model=self._model)
