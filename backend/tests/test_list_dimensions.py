"""Phase 4: list-valued text columns ("French, Japanese, Desserts") and meaning matches for their items."""
from __future__ import annotations

import pandas as pd
import pyarrow as pa

from app.connectors.base import SchemaContext
from app.grounding.values import ValueIndex, corpus_table, snap_intent
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import SemanticRegistry, build_sql_from_intent
from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable
from app.services.intent_mapper import _is_list_filter_value


def _registry(separator=","):
    return SemanticRegistry(SemanticSchema(
        version="1.0", dataset="zomato", description="Restaurants", tables=[SemanticTable(name="zomato")], joins=[],
        metrics=[SemanticMetric(name="restaurants", display_name="Restaurants", description="Restaurants",
                                aggregation="COUNT_DISTINCT", sql_expression="zomato.restaurant_id",
                                required_tables=["zomato"])],
        dimensions=[SemanticDimension(name="cuisines", display_name="Cuisines", sql_expression="zomato.cuisines",
                                      required_tables=["zomato"], cardinality="medium", separator=separator),
                    SemanticDimension(name="city", display_name="City", sql_expression="zomato.city",
                                      required_tables=["zomato"], cardinality="medium")],
        time_dimensions=[]))


ITEMS = "string_to_array(regexp_replace(zomato.cuisines, '\\s*,\\s*', ',', 'g'), ',')"


def test_filters_on_a_list_dimension_match_whole_items():
    registry = _registry()

    def where(operator, value):
        intent = SemanticIntent(metric="restaurants", filters=[FilterCondition(dimension="cuisines", operator=operator,
                                                                               value=value)])
        return build_sql_from_intent(intent, registry).split("WHERE ", 1)[1].split("\n")[0]

    assert where("eq", "Chinese") == f"array_has({ITEMS}, 'Chinese')"
    assert where("ne", "Chinese") == f"NOT array_has({ITEMS}, 'Chinese')"
    assert where("in", ["Chinese", "Thai"]) == f"array_has_any({ITEMS}, make_array('Chinese', 'Thai'))"
    assert where("not_in", ["Thai"]) == f"NOT array_has_any({ITEMS}, make_array('Thai'))"
    assert where("contains", "Asian") == "zomato.cuisines ILIKE '%' || 'Asian' || '%'"  # raw text, by design
    assert where("eq", "it's") == f"array_has({ITEMS}, 'it''s')"


def test_a_breakdown_by_a_list_dimension_counts_each_item_and_groups_by_the_expression():
    sql = build_sql_from_intent(SemanticIntent(metric="restaurants", dimensions=["cuisines"]), _registry())
    assert f"SELECT unnest({ITEMS}) AS cuisines" in sql
    # GROUP BY the alias would bind to the raw column (same name) and group whole lists.
    assert f"GROUP BY unnest({ITEMS})" in sql
    plain = build_sql_from_intent(SemanticIntent(metric="restaurants", dimensions=["cuisines"]), _registry(None))
    assert "unnest" not in plain and "GROUP BY zomato.cuisines" in plain


class _ListProfileConnector:
    """Answers the two list-profiling queries for each text column from a DataFrame."""

    def __init__(self, df):
        self.df = df
        self.queries = []

    def table_ref_resolver(self):
        return None

    def execute_query(self, sql, limit=5000):
        self.queries.append(sql)
        column = next(c for c in self.df.columns if f"zomato.{c}" in sql)
        values = self.df[column].dropna()
        if "COUNT(DISTINCT item)" in sql:
            separator = ";" if "';'" in sql else "|" if "'|'" in sql else ","
            items = {part.strip() for v in values for part in str(v).split(separator)}
            return pd.DataFrame([{"items": len(items)}])
        row = {"n": len(values)}
        for i, s in enumerate((",", ";", "|")):
            row[f"s{i}"] = int(values.astype(str).str.contains(s, regex=False).sum())
        return pd.DataFrame([row])


def test_list_columns_are_detected_and_free_text_is_not():
    from app.services.schema_generator import detect_list_dimensions

    from itertools import combinations

    names = ["French", "Japanese", "Desserts", "Chinese", "Thai", "North Indian", "Pizza", "Cafe"]
    pairs = [f"{a}, {b}" for a, b in combinations(names, 2)]          # 28 distinct lists of 8 items
    cuisines = (pairs + names)[:36] * 2
    addresses = [f"Shop {i}, Sector {i}, Noida" for i in range(72)]
    tags = [f"t{a}; t{b}" for a, b in combinations(range(8), 2)] * 3
    tags = (tags + tags)[:72]
    df = pd.DataFrame({"cuisines": cuisines, "address": addresses, "tags": tags})
    ctx = SchemaContext(tables={"zomato": [{"name": c, "type": "VARCHAR"} for c in df.columns]},
                        row_counts={"zomato": len(df)}, join_paths=[],
                        distinct_counts={"zomato": {c: int(df[c].nunique()) for c in df.columns}})
    schema = _registry(None).schema
    out = detect_list_dimensions(schema, ctx, _ListProfileConnector(df))
    dims = {d.name: d for d in out.dimensions}
    assert dims["cuisines"].separator == "," and dims["cuisines"].cardinality == "low"   # the existing one, marked
    assert "address" not in dims                                                           # every part is new
    assert dims["tags"].separator == ";"                                                   # added
    assert dims["city"].separator is None


class _Similarity:
    """Dense search with similarity scores, for ValueIndex.similar."""

    def __init__(self, scores):
        self.scores = scores

    def encode_query(self, *, model, query):
        self._last = query
        return [0.0]

    def search(self, source, *, query, k, filter=None):
        ranked = sorted(self.scores.get(self._last, {}).items(), key=lambda kv: -kv[1])
        return pa.table({"value_id": [f"cuisines:{v}" for v, _ in ranked], "value": [v for v, _ in ranked],
                         "similarity": [s for _, s in ranked]})


def _index(scores):
    registry = _registry()
    values = {"cuisines": ["Desserts", "Mithai", "Bakery", "Chinese", "Thai", "Seafood"], "city": ["New Delhi"]}
    return ValueIndex(_Similarity(scores), text_source="w_c_gv_x", trigram_source="w_c_gt_x",
                      corpus=corpus_table(values, registry), registry=registry)


def test_an_item_named_word_for_word_stays_exact_and_other_words_match_by_meaning():
    index = _index({"sweets": {"Desserts": 0.71, "Mithai": 0.66, "Bakery": 0.60, "Seafood": 0.41, "Thai": 0.30}})
    intent = SemanticIntent(metric="restaurants", filters=[
        FilterCondition(dimension="cuisines", operator="contains", value="chinese"),
        FilterCondition(dimension="cuisines", operator="contains", value="sweets")])
    outcome = snap_intent(intent, index)
    assert outcome.intent.filters[0] == FilterCondition(dimension="cuisines", operator="eq", value="Chinese")
    assert outcome.intent.filters[1] == FilterCondition(dimension="cuisines", operator="in",
                                                        value=["Desserts", "Mithai", "Bakery"])
    meaning = next(e for e in outcome.evidence if e["text"] == "sweets")
    assert meaning["confidence"] == "meaning" and meaning["scores"]["Desserts"] == 0.71
    assert any("matched by meaning: Desserts, Mithai, Bakery" in line for line in outcome.trace)


def test_nothing_close_in_meaning_leaves_the_filter_as_written():
    index = _index({"spaceships": {"Seafood": 0.31}})
    intent = SemanticIntent(metric="restaurants", filters=[
        FilterCondition(dimension="cuisines", operator="contains", value="spaceships")])
    assert snap_intent(intent, index).intent == intent


def test_a_refused_word_that_filters_a_list_dimension_is_answerable():
    registry = _registry()
    intent = SemanticIntent(metric="restaurants", filters=[
        FilterCondition(dimension="cuisines", operator="contains", value="sweets")])
    assert _is_list_filter_value("sweets", intent, registry)
    assert not _is_list_filter_value("product weight", intent, registry)
    on_city = SemanticIntent(metric="restaurants", filters=[FilterCondition(dimension="city", operator="eq",
                                                                            value="Atlantis")])
    assert not _is_list_filter_value("Atlantis", on_city, registry)  # not a list dimension: may be a real gap


def test_a_named_value_the_llm_dropped_is_added_only_for_name_dimensions():
    from app.agent.nodes.intent_mapper import _filter_named_values

    registry = _registry()
    hint = {"kind": "hint", "dimension": "city", "value": "New Delhi", "exact": True}
    intent = SemanticIntent(metric="restaurants", dimensions=["cuisines"])
    new, notes = _filter_named_values(intent, "Top cuisines in New Delhi", registry, [hint])
    assert new.filters == [FilterCondition(dimension="city", operator="eq", value="New Delhi")] and notes
    # The value already filters something else: left alone.
    used = intent.model_copy(update={"filters": [FilterCondition(dimension="cuisines", operator="eq",
                                                                 value="New Delhi")]})
    assert _filter_named_values(used, "Top cuisines in New Delhi", registry, [hint])[0] == used


def test_explanations_with_stray_cjk_characters_are_rejected():
    from app.agent.nodes.explainer import _foreign_script

    assert _foreign_script("for Price Range = 2, it is 的 3.38", "Price Range = 2: 3.38")
    assert not _foreign_script("for Price Range = 2, it is 3.38", "Price Range = 2: 3.38")


def test_yes_no_values_match_however_they_are_written():
    registry = _registry()
    values = {"cuisines": ["Chinese"], "city": ["Yes", "No"]}  # city stands in for a Yes/No column
    index = ValueIndex(_Similarity({}), text_source="w_c_gv_x", trigram_source="w_c_gt_x",
                       corpus=corpus_table(values, registry), registry=registry)
    assert index.exact("city", "true") == "Yes" and index.exact("city", "1") == "Yes"
    assert index.exact("city", "False") == "No"
    assert index.exact("cuisines", "true") is None  # not a yes/no dimension


def test_true_is_not_mapped_onto_a_numeric_level():
    registry = _registry()
    index = ValueIndex(_Similarity({}), text_source="w_c_gv_x", trigram_source="w_c_gt_x",
                       corpus=corpus_table({"city": ["1", "2", "3", "4"]}, registry), registry=registry)
    assert index.exact("city", "true") is None and index.exact("city", "1") == "1"


def test_a_refusal_naming_a_list_item_is_overruled():
    from app.services.intent_mapper import _names_list_item

    registry = _registry()
    index = ValueIndex(_Similarity({}), text_source="w_c_gv_x", trigram_source="w_c_gt_x",
                       corpus=corpus_table({"cuisines": ["Bakery", "Juices", "Burger"], "city": ["Noida"]}, registry),
                       registry=registry)
    for missing in ("bakeries", "average rating of juice bars", "number of restaurants that serve burgers"):
        assert _names_list_item(missing, index), missing
    assert not _names_list_item("product weight", index)
    assert not _names_list_item("Noida", index)  # city is not a list dimension: a refusal there may be real
    assert not _names_list_item("bakeries", None)


class _Retrieval(_Similarity):
    """Lexical and dense retrieval that rank nothing, for ValueIndex.assumption."""

    def lexical_search(self, source, *, text, k, filter=None):
        return pa.table({"value_id": pa.array([], pa.string())})

    def trigram_search(self, source, *, text, k, filter=None):
        return pa.table({"value_id": pa.array([], pa.string())})

    def search(self, source, *, query, k, filter=None):
        return pa.table({"value_id": ["city:No", "city:Yes"], "value": ["No", "Yes"], "similarity": [0.5, 0.4]})

    def rrf_fuse(self, rankings):
        return [(value_id, 1.0) for value_id in dict.fromkeys(v for r in rankings for v in r)]


def test_a_yes_no_value_is_never_questioned_for_not_being_named():
    registry = _registry()
    index = ValueIndex(_Retrieval({}), text_source="w_c_gv_x", trigram_source="w_c_gt_x",
                       corpus=corpus_table({"city": ["Yes", "No"]}, registry), registry=registry)
    intent = SemanticIntent(metric="restaurants", filters=[FilterCondition(dimension="city", operator="eq", value="Yes")])
    outcome = snap_intent(intent, index, question="How many restaurants have online delivery?")
    assert outcome.unresolved == [] and outcome.intent == intent
    assert index.is_yes_no("city") and not index.is_yes_no("cuisines")


def test_a_refusal_naming_a_list_item_by_meaning_is_overruled_but_real_gaps_are_not():
    from app.services.intent_mapper import _names_list_item

    registry = _registry()
    # Scores measured on the restaurant file's cuisines (Phase 4).
    index = _index({"steakhouses": {"Seafood": 0.81}, "coffee": {"Bakery": 0.89}, "revenue": {"Thai": 0.63},
                    "menu prices": {"Chinese": 0.59}, "restaurants": {"Bakery": 0.9}})
    assert _names_list_item("steakhouses", index, registry)
    assert _names_list_item("average rating of coffee places", index, registry)
    assert not _names_list_item("revenue", index, registry)
    assert not _names_list_item("menu prices", index, registry)
    # A word of the data model's own names says what is counted, not which item.
    registry.schema.metrics[0].display_name = "Restaurants"
    assert not _names_list_item("number of restaurants", index, registry)


def test_a_refusal_naming_only_what_the_intent_has_is_overruled():
    from app.services.intent_mapper import _intent_covers
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable

    registry = SemanticRegistry(SemanticSchema(
        version="1.0", dataset="store", description="d", tables=[SemanticTable(name="track")], joins=[],
        metrics=[SemanticMetric(name="track_count", display_name="Track Count", description="Tracks",
                                aggregation="COUNT_DISTINCT", sql_expression="track.track_id", required_tables=["track"])],
        dimensions=[SemanticDimension(name=n, display_name=n.replace("_", " ").title(), sql_expression=f"track.{n}",
                                      required_tables=["track"], cardinality="low")
                    for n in ("genre_name", "playlist_name")],
        time_dimensions=[]))
    by_genre = SemanticIntent(metric="track_count", dimensions=["genre_name"])
    assert _intent_covers("number of tracks by genre", by_genre, registry)
    grunge = SemanticIntent(metric="track_count", filters=[
        FilterCondition(dimension="playlist_name", operator="eq", value="Grunge")])
    assert _intent_covers("tracks in the Grunge playlist", grunge, registry)
    # A word the intent does not have keeps the refusal.
    assert not _intent_covers("track length", SemanticIntent(metric="track_count"), registry)
    assert not _intent_covers("average age of customers", SemanticIntent(metric="track_count"), registry)
    assert not _intent_covers("profit by genre", by_genre, registry)


class _AgreeingRetrieval(_Retrieval):
    """Every retriever ranks one value first, whatever the question."""

    def __init__(self, value_id):
        super().__init__({})
        self.value_id = value_id

    def lexical_search(self, source, *, text, k, filter=None):
        return pa.table({"value_id": [self.value_id]})

    def search(self, source, *, query, k, filter=None):
        dimension, value = self.value_id.split(":", 1)
        return pa.table({"value_id": [self.value_id], "value": [value], "similarity": [0.6]})


def test_a_value_matching_only_the_dimension_label_is_not_a_hint():
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable

    registry = SemanticRegistry(SemanticSchema(
        version="1.0", dataset="store", description="d", tables=[SemanticTable(name="artist")], joins=[],
        metrics=[SemanticMetric(name="n", display_name="N", description="N", aggregation="COUNT",
                                sql_expression="artist.id", required_tables=["artist"])],
        dimensions=[SemanticDimension(name="artist_name", display_name="Artist Name", sql_expression="artist.name",
                                      required_tables=["artist"], cardinality="high")],
        time_dimensions=[]))
    corpus = corpus_table({"artist_name": ["Various Artists", "Iron Maiden"]}, registry)
    index = ValueIndex(_AgreeingRetrieval("artist_name:Various Artists"), text_source="w_c_gv_x",
                       trigram_source="w_c_gt_x", corpus=corpus, registry=registry)
    assert index.candidates("Show Total Invoice Amount by Artist Name") == []
    named = index.candidates("Revenue from Various Artists compilations")
    assert [m.value for m in named] == ["Various Artists"]  # "various" is not a label word


def test_revenue_and_paid_are_what_a_payment_amount_measures():
    from app.services.intent_mapper import _intent_covers
    from app.semantic.models import SemanticDimension, SemanticMetric, SemanticSchema, SemanticTable

    registry = SemanticRegistry(SemanticSchema(
        version="1.0", dataset="rental", description="d", tables=[SemanticTable(name="payment")], joins=[],
        metrics=[SemanticMetric(name="total_payment_amount", display_name="Total Payment Amount", description="p",
                                aggregation="SUM", sql_expression="payment.amount", required_tables=["payment"]),
                 SemanticMetric(name="rentals", display_name="Rentals", description="r",
                                aggregation="COUNT_DISTINCT", sql_expression="payment.rental_id", required_tables=["payment"])],
        dimensions=[SemanticDimension(name="customer_name", display_name="Customer Name", sql_expression="payment.c",
                                      required_tables=["payment"], cardinality="high")],
        time_dimensions=[]))
    revenue = SemanticIntent(metric="total_payment_amount", dimensions=["customer_name"])
    assert _intent_covers("total revenue", revenue, registry)
    assert _intent_covers("customers who paid the most", revenue, registry)
    assert not _intent_covers("total revenue", SemanticIntent(metric="rentals"), registry)  # a count is not money
    assert not _intent_covers("profit", revenue, registry)


def test_a_small_set_of_lists_is_a_list_when_most_rows_hold_the_separator():
    from itertools import combinations

    from app.services.schema_generator import detect_list_dimensions

    features = ["Trailers", "Commentaries", "Deleted Scenes", "Behind the Scenes"]
    combos = [", ".join(c) for n in (1, 2, 3, 4) for c in combinations(features, n)]  # 15 distinct lists
    labels = ["Books", "Toys", "Garden", "Music", "Films", "Games", "Food", "Tools", "Shoes", "Home, Kitchen"]
    df = pd.DataFrame({"cuisines": (combos * 5)[:60], "city": (labels * 6)[:60]})
    ctx = SchemaContext(tables={"zomato": [{"name": c, "type": "VARCHAR"} for c in df.columns]},
                        row_counts={"zomato": len(df)}, join_paths=[],
                        distinct_counts={"zomato": {c: int(df[c].nunique()) for c in df.columns}})
    out = detect_list_dimensions(_registry(None).schema, ctx, _ListProfileConnector(df))
    dims = {d.name: d for d in out.dimensions}
    assert dims["cuisines"].separator == ","  # 15 distinct values, most with commas
    assert dims["city"].separator is None  # 10 labels, one with a comma: a category


def test_a_named_list_item_the_llm_dropped_is_added_even_on_a_small_list():
    from app.agent.nodes.intent_mapper import _filter_named_values

    registry = _registry()
    schema = registry.schema
    dims = [d.model_copy(update={"cardinality": "low"}) if d.name == "cuisines" else d for d in schema.dimensions]
    registry = SemanticRegistry(schema.model_copy(update={"dimensions": dims}))
    hint = {"kind": "hint", "dimension": "cuisines", "value": "Desserts", "exact": True}
    new, notes = _filter_named_values(SemanticIntent(metric="restaurants"), "How many restaurants serve desserts?",
                                      registry, [hint])
    assert new.filters == [FilterCondition(dimension="cuisines", operator="eq", value="Desserts")] and notes
