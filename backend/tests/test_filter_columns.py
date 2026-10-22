"""Conditions on a column's value: "How many tracks are longer than 5 minutes?" (track.milliseconds >
300000), "How many rentals have not been returned?" (rental.return_date IS NULL). Before, only breakdown
columns could be filtered, and such questions were refused or answered without the condition."""
from __future__ import annotations

import pytest

from app.connectors.base import SchemaContext
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import SemanticRegistry, build_sql_from_intent
from app.semantic.models import (SemanticDimension, SemanticFilterColumn, SemanticMetric, SemanticSchema,
                                 SemanticTable)
from app.services.intent_mapper import IntentValidationError, validate_semantic_intent
from app.services.schema_completion import SchemaProposal, complete_schema

REGISTRY = SemanticRegistry(SemanticSchema(
    version="1.0", dataset="d", description="d", time_dimensions=[], joins=[],
    tables=[SemanticTable(name="track"), SemanticTable(name="rental")],
    metrics=[SemanticMetric(name="tracks", display_name="Number of Tracks", description="n", aggregation="COUNT_DISTINCT",
                            sql_expression="track.track_id", required_tables=["track"]),
             SemanticMetric(name="rentals", display_name="Number of Rentals", description="n", aggregation="COUNT",
                            sql_expression="rental.rental_id", required_tables=["rental"])],
    dimensions=[SemanticDimension(name="genre", display_name="Genre", sql_expression="track.genre",
                                  required_tables=["track"])],
    filter_columns=[SemanticFilterColumn(name="milliseconds", display_name="Milliseconds",
                                         sql_expression="track.milliseconds", required_tables=["track"],
                                         kind="number", unit="milliseconds"),
                    SemanticFilterColumn(name="return_date", display_name="Return Date",
                                         sql_expression="rental.return_date", required_tables=["rental"], kind="date")]))
SCHEMA = SchemaContext(tables={"track": [], "rental": []}, row_counts={}, join_paths=[])


def _condition(dimension, operator, value=None):
    return FilterCondition(dimension=dimension, operator=operator, value=value)


def test_a_condition_on_a_number_or_a_missing_date_compiles():
    longer = validate_semantic_intent(SemanticIntent(metric="tracks", filters=[_condition("milliseconds", "gt", 300000)]),
                                      REGISTRY, SCHEMA)
    assert "WHERE track.milliseconds > 300000" in build_sql_from_intent(longer, REGISTRY)
    open_rentals = validate_semantic_intent(SemanticIntent(metric="rentals", filters=[_condition("return_date", "is_null")]),
                                            REGISTRY, SCHEMA)
    assert "WHERE rental.return_date IS NULL" in build_sql_from_intent(open_rentals, REGISTRY)


@pytest.mark.parametrize("intent,reason", [
    (SemanticIntent(metric="tracks", dimensions=["milliseconds"]), "column filter"),       # never a breakdown
    (SemanticIntent(metric="tracks", filters=[_condition("milliseconds", "gt", "long")]), "numbers in milliseconds"),
    (SemanticIntent(metric="tracks", filters=[_condition("milliseconds", "gt")]), "needs a value"),
    (SemanticIntent(metric="rentals", filters=[_condition("return_date", "lt", "soon")]), "ISO date"),
])
def test_conditions_that_would_compile_to_wrong_sql_are_rejected(intent, reason):
    with pytest.raises(IntentValidationError, match=reason):
        validate_semantic_intent(intent, REGISTRY, SCHEMA)


def test_the_explanation_states_the_condition_in_the_columns_unit():
    from app.agent.nodes.explainer import describe_computed

    longer = SemanticIntent(metric="tracks", filters=[_condition("milliseconds", "gt", 300000)])
    assert describe_computed(longer, REGISTRY) == "Number of Tracks, where Milliseconds is more than 300000 milliseconds"
    open_rentals = SemanticIntent(metric="rentals", filters=[_condition("return_date", "is_null")])
    assert describe_computed(open_rentals, REGISTRY) == "Number of Rentals, where Return Date is missing"


def test_numbers_and_dates_of_metric_tables_become_filter_columns():
    tables = {
        "track": [("track_id", "INTEGER"), ("album_id", "INTEGER"), ("name", "VARCHAR"), ("milliseconds", "INTEGER"),
                  ("bytes", "INTEGER"), ("unit_price", "DECIMAL(10,2)")],
        "rental": [("rental_id", "INTEGER"), ("rental_date", "TIMESTAMP"), ("return_date", "TIMESTAMP"),
                   ("last_update", "TIMESTAMP")],
    }
    ctx = SchemaContext(tables={t: [{"name": n, "type": ty} for n, ty in cols] for t, cols in tables.items()},
                        row_counts={"track": 3503, "rental": 16044}, join_paths=[],
                        distinct_counts={"track": {"milliseconds": 3080, "bytes": 3501, "unit_price": 2, "name": 3257},
                                         "rental": {"rental_date": 15815, "return_date": 15836}})
    proposal = SchemaProposal.model_validate({"dataset": "d", "metrics": [
        {"name": "tracks", "aggregation": "COUNT_DISTINCT", "sql_expression": "track.track_id"},
        {"name": "rentals", "aggregation": "COUNT", "sql_expression": "rental.rental_id"}]})
    schema, notes = complete_schema(proposal, ctx)
    columns = {c.name: c for c in schema.filter_columns}
    assert columns["milliseconds"].kind == "number" and columns["milliseconds"].unit == "milliseconds"
    assert "length or duration" in columns["milliseconds"].description
    assert columns["bytes"].unit == "bytes"
    assert columns["return_date"].kind == "date" and "not happened yet" in columns["return_date"].description
    assert not {"track_id", "album_id", "last_update", "name"} & set(columns)   # keys, audit stamps, text
    # A date that is already the time dimension is not repeated as a filter column.
    assert {t.sql_expression for t in schema.time_dimensions} == {"rental.rental_date"}
    assert not any(c.sql_expression == "rental.rental_date" for c in columns.values())
    names = [m.name for m in schema.metrics] + [d.name for d in schema.dimensions] + \
            [t.name for t in schema.time_dimensions] + list(columns)
    assert len(names) == len(set(names))
    registry = SemanticRegistry(schema)
    assert registry.is_filter_only("milliseconds") and registry.get_dimension("milliseconds").unit == "milliseconds"


def test_a_column_used_by_a_join_is_a_key_not_a_condition():
    ctx = SchemaContext(tables={"employee": [{"name": "employee_id", "type": "INTEGER"}, {"name": "reports_to", "type": "INTEGER"},
                                             {"name": "salary", "type": "DOUBLE"}]},
                        row_counts={"employee": 8}, distinct_counts={"employee": {"reports_to": 3, "salary": 8}},
                        join_paths=[{"from": "employee", "to": "employee", "type": "LEFT",
                                     "on": "employee.reports_to = employee.employee_id"}])
    proposal = SchemaProposal.model_validate({"dataset": "d", "metrics": [
        {"name": "employees", "aggregation": "COUNT", "sql_expression": "employee.employee_id"}]})
    schema, _ = complete_schema(proposal, ctx)
    expressions = {c.sql_expression for c in schema.filter_columns}
    assert "employee.salary" in expressions and "employee.reports_to" not in expressions


def test_an_answer_written_as_text_instead_of_a_tool_call_is_recovered():
    """qwen3:4b over Ollama, on a long prompt: tool_calls=None, the JSON in the message text, and
    Instructor failing with "does not support multiple tool calls"."""
    from types import SimpleNamespace

    from app.llm import _answer_in_content

    def failure(content, tool_calls=None):
        message = SimpleNamespace(content=content, tool_calls=tool_calls)
        exc = RuntimeError("Instructor does not support multiple tool calls, use List[Model] instead")
        exc.last_completion = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None, model="m")
        return exc

    text = 'Here it is: {"metric": "tracks", "filters": [{"dimension": "milliseconds", "operator": "gt", "value": 300000}]}'
    recovered = _answer_in_content(failure(text), SemanticIntent)
    assert recovered.metric == "tracks" and recovered.filters[0].value == 300000
    wrapped = _answer_in_content(failure('{"question": "q", "intent": {"metric": "tracks"}}'), SemanticIntent)
    assert wrapped.metric == "tracks"
    # Not the object asked for, a real tool call, or no completion at all: the original error stands.
    assert _answer_in_content(failure('{"answer": 42}'), SemanticIntent) is None
    assert _answer_in_content(failure(text, tool_calls=[object()]), SemanticIntent) is None
    assert _answer_in_content(RuntimeError("timeout"), SemanticIntent) is None
