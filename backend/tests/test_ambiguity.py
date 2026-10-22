"""Phase 4: the agent asks when the data makes a question ambiguous (amounts in several currencies)."""
from __future__ import annotations

import pandas as pd

from app.agent import QueryGraphDependencies, QueryGraphRunner
from app.agent.ambiguity import BREAK_DOWN, apply_choice, mixed_units
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import SemanticRegistry
from app.services import IntentMapperConfig, IntentMapperRouter

from test_agent_conversation import LLM_CONFIG, ScriptedLLM
from test_query_graph import StubConnector, _load_registry


def _registry_with_unit(unit="payment_type"):
    """The demo registry, pretending revenue is recorded in the units held by `unit`."""
    schema = _load_registry().schema
    metrics = [m.model_copy(update={"unit_dimension": unit}) if m.name == "total_revenue" else m
               for m in schema.metrics]
    return SemanticRegistry(schema.model_copy(update={"metrics": metrics}))


def _probe(rows):
    calls = []

    def probe(intent):
        calls.append(intent)
        return rows

    probe.calls = calls
    return probe


def test_one_figure_over_several_units_asks_with_a_breakdown_first():
    registry = _registry_with_unit()
    probe = _probe([{"payment_type": "credit_card", "metric_value": 5.0}, {"payment_type": "boleto", "metric_value": 2.0}])
    pending = mixed_units(SemanticIntent(metric="total_revenue"), registry, probe)
    assert pending["kind"] == "choice" and pending["reason"] == "mixed_units"
    assert "2 different" in pending["prompt"]
    assert pending["choices"][0]["label"] == "Break down by Payment Method"
    assert {c["label"] for c in pending["choices"][1:]} == {"boleto", "credit_card"}
    assert probe.calls[0].dimensions == ["payment_type"]  # the probe adds the unit to the breakdown


def test_units_with_more_rows_are_offered_first():
    from app.agent.ambiguity import _by_rows

    registry = _registry_with_unit()
    counts = _probe([{"payment_type": "boleto", "metric_value": 22}, {"payment_type": "credit_card", "metric_value": 8652}])
    ordered = _by_rows(["boleto", "credit_card"], SemanticIntent(metric="total_revenue"), registry, "payment_type", counts)
    if counts.calls:  # the demo registry has a count metric on the revenue table
        assert ordered == ["credit_card", "boleto"]
    else:
        assert ordered == ["boleto", "credit_card"]


def test_groups_with_one_unit_each_pass_with_a_note():
    """Per-country averages are fine (one currency per country), but the answer says which unit."""
    registry = _registry_with_unit()
    rows = [{"customer_state": "SP", "payment_type": "credit_card", "metric_value": 1.0},
            {"customer_state": "RJ", "payment_type": "boleto", "metric_value": 1.0}]
    intent = SemanticIntent(metric="total_revenue", dimensions=["customer_state"])
    note = mixed_units(intent, registry, _probe(rows))
    assert note["kind"] == "note" and "Each row is in its own Payment Method" in note["text"]
    one = mixed_units(intent, registry, _probe(rows[:1]))
    assert one == {"kind": "note", "dimension": "payment_type", "text": "Amounts are in credit_card."}
    mixed = rows + [{"customer_state": "SP", "payment_type": "boleto", "metric_value": 1.0}]
    assert mixed_units(intent, registry, _probe(mixed))["kind"] == "choice"


def test_no_question_when_the_unit_is_fixed_or_broken_down_or_absent():
    registry = _registry_with_unit()
    never = _probe([])
    fixed = SemanticIntent(metric="total_revenue", filters=[FilterCondition(dimension="payment_type", operator="eq",
                                                                            value="boleto")])
    assert mixed_units(fixed, registry, never) == {"kind": "note", "dimension": "payment_type", "text": "Amounts are in boleto."}
    assert mixed_units(SemanticIntent(metric="total_revenue", dimensions=["payment_type"]), registry, never) is None
    assert mixed_units(SemanticIntent(metric="order_count"), registry, never) is None  # no unit on this metric
    assert never.calls == []


def test_a_choice_patches_the_intent():
    pending = {"choices": [{"id": BREAK_DOWN, "label": "By", "patch": {"add_dimension": "payment_type"}},
                           {"id": "0", "label": "boleto", "patch": {"add_filter": {
                               "dimension": "payment_type", "operator": "eq", "value": "boleto"}}}]}
    intent = SemanticIntent(metric="total_revenue", dimensions=["customer_state"])
    by, _ = apply_choice(intent, pending, BREAK_DOWN)
    assert by.dimensions == ["customer_state", "payment_type"]
    one, label = apply_choice(intent, pending, "0")
    assert one.filters == [FilterCondition(dimension="payment_type", operator="eq", value="boleto")] and label == "boleto"
    assert apply_choice(intent, pending, "nonsense")[0].dimensions == by.dimensions  # unknown: never mix


class _UnitConnector(StubConnector):
    """Two payment types when the query breaks down by payment type; one row otherwise."""

    def execute_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        self.executed_sql.append(sql)
        if "payment_type" in sql.split("FROM")[0]:
            return pd.DataFrame([{"payment_type": "boleto", "metric_value": 2.0},
                                 {"payment_type": "credit_card", "metric_value": 5.0}])
        return pd.DataFrame([{"metric_value": 7.0}])


def test_the_agent_pauses_on_mixed_units_and_continues_with_the_choice():
    registry = _registry_with_unit()
    connector = _UnitConnector()
    runner = QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=registry,
        intent_mapper=IntentMapperRouter(config=LLM_CONFIG, llm_mapper=ScriptedLLM(SemanticIntent(metric="total_revenue")),
                                         value_index=lambda: None),
        intent_config=IntentMapperConfig(mode="heuristic")))

    paused = runner.invoke(question="total revenue", query_id="q1", thread_id="t")
    assert paused["clarification"]["kind"] == "choice"
    probes = len(connector.executed_sql)

    boleto = next(c["id"] for c in paused["clarification"]["choices"] if c["label"] == "boleto")
    done = runner.resume(thread_id="t", answer=boleto)
    assert done["intent"].filters == [FilterCondition(dimension="payment_type", operator="eq", value="boleto")]
    assert "payment_type = 'boleto'" in connector.executed_sql[-1]
    assert len(connector.executed_sql) == probes + 1  # no second question: the patched intent is unambiguous
    assert any(e.get("kind") == "clarification" and e.get("value") == "boleto" for e in done["evidence"])


def _store_registry(*, line_amount=True, relationship="many_to_one"):
    """invoice (one) -> invoice_line (many) -> track -> genre, as Chinook declares them."""
    from app.semantic.models import SemanticDimension, SemanticJoin, SemanticMetric, SemanticSchema, SemanticTable

    def metric(name, label, aggregation, expression):
        return SemanticMetric(name=name, display_name=label, description=label, aggregation=aggregation,
                              sql_expression=expression, required_tables=[expression.split(".")[0]])

    def join(frm, to, on):
        return SemanticJoin.model_validate({"from": frm, "to": to, "on": on, "relationship": relationship})

    metrics = [metric("total_invoice_amount", "Total Invoice Amount", "SUM", "invoice.total"),
               metric("invoices", "Number of Invoices", "COUNT_DISTINCT", "invoice.invoice_id"),
               metric("total_quantity", "Total Quantity", "SUM", "invoice_line.quantity")]
    if line_amount:
        metrics.append(metric("total_invoice_line_amount", "Total Invoice Line Amount", "SUM",
                              "invoice_line.unit_price * invoice_line.quantity"))
    return SemanticRegistry(SemanticSchema(
        version="1.0", dataset="store", description="d",
        tables=[SemanticTable(name=t) for t in ("invoice", "invoice_line", "track", "genre")],
        joins=[join("invoice_line", "invoice", "invoice_line.invoice_id = invoice.invoice_id"),
               join("invoice_line", "track", "invoice_line.track_id = track.track_id"),
               join("track", "genre", "track.genre_id = genre.genre_id")],
        metrics=metrics,
        dimensions=[SemanticDimension(name="genre_name", display_name="Genre", sql_expression="genre.name",
                                      required_tables=["genre"], cardinality="low"),
                    SemanticDimension(name="billing_country", display_name="Country",
                                      sql_expression="invoice.billing_country", required_tables=["invoice"],
                                      cardinality="low")],
        time_dimensions=[]))


def test_a_total_repeated_across_its_lines_asks_for_a_line_level_measure():
    from app.agent.ambiguity import fan_out

    registry = _store_registry()
    by_genre = SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"])
    # One money measure per line with the same grand total: used, with the reason, instead of asking.
    substitute = fan_out(by_genre, registry, _probe([{"metric_value": 2328.6}]))
    assert substitute["kind"] == "substitute" and substitute["patch"] == {"set_metric": "total_invoice_line_amount"}
    assert "count each invoice several times" in substitute["text"]
    # Different totals (shipping fees are not line sales), or no way to tell: ask instead.
    totals = iter([{"metric_value": 2138.0}, {"metric_value": 68137.0}])
    assert fan_out(by_genre, registry, lambda intent: [next(totals)])["kind"] == "choice"
    assert fan_out(by_genre, registry, _probe([]))["kind"] == "choice"
    # Two money measures per line: the user picks.
    from app.semantic.models import SemanticMetric

    extra = SemanticMetric(name="total_line_price", display_name="Total Line Price", description="p",
                           aggregation="SUM", sql_expression="invoice_line.unit_price", required_tables=["invoice_line"])
    registry = SemanticRegistry(registry.schema.model_copy(update={"metrics": [*registry.schema.metrics, extra]}))
    pending = fan_out(by_genre, registry, _probe([]))
    assert pending["kind"] == "choice" and pending["reason"] == "fan_out"
    assert {c["label"] for c in pending["choices"][:2]} == {"Total Invoice Line Amount", "Total Line Price"}
    patched, label = apply_choice(by_genre, pending, next(c["id"] for c in pending["choices"]
                                                          if c["label"] == "Total Invoice Line Amount"))
    assert patched.metric == "total_invoice_line_amount" and label == "Total Invoice Line Amount"
    assert fan_out(patched, registry, _probe([])) is None
    # A filter fans out the same way ("revenue from rock tracks").
    rock = SemanticIntent(metric="total_invoice_amount",
                          filters=[FilterCondition(dimension="genre_name", operator="eq", value="Rock")])
    assert fan_out(rock, registry, _probe([]))["reason"] == "fan_out"


def test_no_fan_out_for_counts_of_distinct_keys_for_one_side_breakdowns_or_unknown_relationships():
    from app.agent.ambiguity import fan_out

    registry = _store_registry()
    assert fan_out(SemanticIntent(metric="invoices", dimensions=["genre_name"]), registry, _probe([])) is None
    assert fan_out(SemanticIntent(metric="total_invoice_amount", dimensions=["billing_country"]),
                   registry, _probe([])) is None
    assert fan_out(SemanticIntent(metric="total_quantity", dimensions=["genre_name"]), registry, _probe([])) is None
    unknown = _store_registry(relationship=None)  # a hand-written schema: not checked
    assert fan_out(SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"]),
                   unknown, _probe([])) is None


def test_with_no_line_level_measure_the_question_is_refused_not_inflated():
    from app.agent.ambiguity import fan_out

    registry = _store_registry(line_amount=False)
    found = fan_out(SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"]), registry, _probe([]))
    # total_quantity is on the line table but not a money measure: it is offered only as a SUM alternative.
    assert found["kind"] == "choice" and [c["label"] for c in found["choices"]] == ["Total Quantity"]
    alone = SemanticRegistry(registry.schema.model_copy(update={
        "metrics": [m for m in registry.schema.metrics if m.name != "total_quantity"]}))
    refused = fan_out(SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"]), alone, _probe([]))
    assert refused["kind"] == "refuse" and "count each invoice several times" in refused["text"]


class _StoreConnector(StubConnector):
    def get_schema(self):
        from app.connectors.base import SchemaContext

        columns = {"invoice": ["invoice_id", "total", "billing_country"], "invoice_line": ["invoice_id", "track_id",
                   "unit_price", "quantity"], "track": ["track_id", "genre_id"], "genre": ["genre_id", "name"]}
        return SchemaContext(tables={t: [{"name": c, "type": "VARCHAR"} for c in cols] for t, cols in columns.items()},
                             row_counts={t: 10 for t in columns}, join_paths=[])


def test_a_refused_fan_out_ends_the_turn_with_the_reason_and_no_query():
    registry = _store_registry(line_amount=False)
    registry = SemanticRegistry(registry.schema.model_copy(update={
        "metrics": [m for m in registry.schema.metrics if m.name != "total_quantity"]}))
    connector = _StoreConnector()
    runner = QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=registry,
        intent_mapper=IntentMapperRouter(config=LLM_CONFIG, llm_mapper=ScriptedLLM(
            SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"])), value_index=lambda: None),
        intent_config=IntentMapperConfig(mode="heuristic")))

    done = runner.invoke(question="revenue by genre", query_id="q1")

    assert "count each invoice several times" in done["unsupported"]
    assert connector.executed_sql == []  # nothing inflated was ever run


def test_a_fan_out_choice_fits_the_api_response():
    """The first live run returned 400: the API model allowed only value and unit clarifications."""
    from app.agent.ambiguity import fan_out
    from app.models.responses import Clarification

    registry = _store_registry(line_amount=False)  # only a non-money alternative: a choice
    pending = fan_out(SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"]), registry, _probe([]))
    clarification = Clarification.model_validate(pending)
    assert clarification.reason == "fan_out" and clarification.dimension is None
    assert clarification.choices[0].label == "Total Quantity"


def test_an_intent_whose_tables_do_not_join_is_rejected_for_repair():
    """On Chinook, revenue by employee had no join path: it failed at compile time as a bare 400."""
    import pytest

    from app.semantic.models import SemanticDimension
    from app.services.intent_mapper import IntentValidationError, validate_semantic_intent

    registry = _store_registry()
    schema = registry.schema.model_copy(update={
        "tables": [*registry.schema.tables, type(registry.schema.tables[0])(name="employee")],
        "dimensions": [*registry.schema.dimensions, SemanticDimension(
            name="employee_name", display_name="Employee", sql_expression="employee.name",
            required_tables=["employee"], cardinality="low")]})
    registry = SemanticRegistry(schema)
    ctx = _StoreConnector().get_schema()
    ctx.tables["employee"] = [{"name": "name", "type": "VARCHAR"}]
    with pytest.raises(IntentValidationError, match="join"):
        validate_semantic_intent(SemanticIntent(metric="total_invoice_amount", dimensions=["employee_name"]),
                                 registry, ctx)
    validate_semantic_intent(SemanticIntent(metric="total_invoice_amount", dimensions=["billing_country"]),
                             registry, ctx)


def test_suggested_questions_never_propose_a_fan_out_or_an_unjoinable_pair():
    from app.semantic.suggestions import suggested_questions

    suggestions = suggested_questions(_store_registry())
    assert suggestions and len(suggestions) <= 6
    assert "Total Invoice Amount by Genre" not in suggestions  # each invoice would count once per line
    assert "Total Invoice Line Amount by Genre" in suggestions
    assert not any("2018" in s for s in suggestions)


def test_the_agent_substitutes_the_line_amount_and_says_why():
    connector = _StoreConnector()  # every probe answers 7.0: the totals agree
    runner = QueryGraphRunner(QueryGraphDependencies(
        connector=connector, schema_context=connector.get_schema(), registry=_store_registry(),
        intent_mapper=IntentMapperRouter(config=LLM_CONFIG, llm_mapper=ScriptedLLM(
            SemanticIntent(metric="total_invoice_amount", dimensions=["genre_name"])), value_index=lambda: None),
        intent_config=IntentMapperConfig(mode="heuristic")))

    done = runner.invoke(question="revenue by genre", query_id="q1")

    assert done["intent"].metric == "total_invoice_line_amount" and "clarification" not in done
    assert any(e.get("kind") == "note" and e["text"].startswith("Used Total Invoice Line Amount") for e in done["evidence"])


def _film_registry(link=True):
    """payment -> rental -> inventory -> film <- film_category -> category, as Pagila declares them."""
    from app.semantic.models import SemanticDimension, SemanticJoin, SemanticMetric, SemanticSchema, SemanticTable

    def join(frm, to, column):
        return SemanticJoin.model_validate({"from": frm, "to": to, "on": f"{frm}.{column} = {to}.{column}",
                                            "relationship": "many_to_one"})

    return SemanticRegistry(SemanticSchema(
        version="1.0", dataset="rental", description="d",
        tables=[SemanticTable(name=t, link=(link and t == "film_category"))
                for t in ("payment", "rental", "inventory", "film", "film_category", "category")],
        joins=[join("payment", "rental", "rental_id"), join("rental", "inventory", "inventory_id"),
               join("inventory", "film", "film_id"), join("film_category", "film", "film_id"),
               join("film_category", "category", "category_id")],
        metrics=[SemanticMetric(name="revenue", display_name="Revenue", description="r", aggregation="SUM",
                                sql_expression="payment.amount", required_tables=["payment"]),
                 SemanticMetric(name="average_rental_rate", display_name="Average Rental Rate", description="r",
                                aggregation="AVG", sql_expression="film.rental_rate", required_tables=["film"])],
        dimensions=[SemanticDimension(name="category_name", display_name="Category", sql_expression="category.name",
                                      required_tables=["category"], cardinality="low")],
        time_dimensions=[]))


def test_grouping_through_a_link_table_is_not_a_fan_out():
    from app.agent.ambiguity import fan_out

    for metric in ("revenue", "average_rental_rate"):
        intent = SemanticIntent(metric=metric, dimensions=["category_name"])
        assert fan_out(intent, _film_registry(), _probe([])) is None, metric
        # Without the link flag the same path is a fan-out (film_category could hold anything).
        assert fan_out(intent, _film_registry(link=False), _probe([])) is not None, metric


def test_link_tables_are_recognised_from_their_columns():
    from app.connectors.base import SchemaContext
    from app.services.schema_completion import _link_tables

    many = {"relationship": "many_to_one", "type": "LEFT"}
    ctx = SchemaContext(
        tables={"film_category": [{"name": "film_id", "type": "INTEGER"}, {"name": "category_id", "type": "INTEGER"},
                                  {"name": "last_update", "type": "TIMESTAMP"}],
                "invoice_line": [{"name": "invoice_id", "type": "INTEGER"}, {"name": "track_id", "type": "INTEGER"},
                                 {"name": "unit_price", "type": "DECIMAL(10,2)"}],
                "film": [{"name": "film_id", "type": "INTEGER"}], "category": [{"name": "category_id", "type": "INTEGER"}],
                "invoice": [{"name": "invoice_id", "type": "INTEGER"}], "track": [{"name": "track_id", "type": "INTEGER"}]},
        row_counts={}, inferred_joins=[],
        join_paths=[{**many, "from": "film_category", "to": "film", "on": "film_category.film_id = film.film_id"},
                    {**many, "from": "film_category", "to": "category", "on": "film_category.category_id = category.category_id"},
                    {**many, "from": "invoice_line", "to": "invoice", "on": "invoice_line.invoice_id = invoice.invoice_id"},
                    {**many, "from": "invoice_line", "to": "track", "on": "invoice_line.track_id = track.track_id"}])
    assert _link_tables(ctx) == {"film_category"}  # invoice_line holds a price: not a link
