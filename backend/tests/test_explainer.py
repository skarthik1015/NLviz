from __future__ import annotations

from pathlib import Path

from app.agent.nodes.explainer import _facts, _grounded, _template_explanation, describe_computed
from app.models import SemanticIntent
from app.models.semantic_intent import FilterCondition
from app.semantic import load_semantic_registry

REGISTRY = load_semantic_registry(Path(__file__).resolve().parents[1] / "app" / "semantic" / "schemas" / "ecommerce.yaml")


def test_describes_what_was_computed_in_the_registry_words():
    intent = SemanticIntent(metric="average_order_value", dimensions=["product_category"], time_dimension="order_date",
                            time_granularity="month", start_date="2018-01-01", end_date="2018-12-31",
                            filters=[FilterCondition(dimension="payment_type", operator="eq", value="credit_card")])
    text = describe_computed(intent, REGISTRY)
    assert text.startswith("Average Order Value by Product Category, month by Order Date")
    assert "where Payment Method eq credit_card" in text and "from 2018-01-01 to 2018-12-31" in text


def test_facts_carry_the_real_figures_including_single_row_results():
    assert _facts([{"metric_value": 99441}], 1) == "Result: 99,441"  # not "Total": the metric may be an average
    facts = _facts([{"customer_state": "SP", "metric_value": 5998226.96}, {"customer_state": "RJ", "metric_value": 2144379.69}],
                   27, registry=REGISTRY)
    assert "Customer State = SP: 5,998,226.96" in facts
    assert "Highest: Customer State = SP (5,998,226.96). Lowest: Customer State = RJ (2,144,379.69)" in facts


def test_lowest_comes_from_every_row_not_the_five_shown():
    rows = [{"customer_state": s, "metric_value": v} for s, v in
            [("SP", 900), ("RJ", 800), ("MG", 700), ("RS", 600), ("PR", 500), ("RR", 3)]]
    facts = _facts(rows, 6, registry=REGISTRY)
    assert "RR: 3" not in facts.split("Highest")[0]  # not among the five rows listed
    assert "Lowest: Customer State = RR (3)" in facts


def test_small_breakdowns_of_counts_show_each_share():
    intent = SemanticIntent(metric="order_count", dimensions=["order_status"])
    facts = _facts([{"order_status": "No", "metric_value": 7100}, {"order_status": "Yes", "metric_value": 2451}],
                   2, intent, REGISTRY)
    assert "Order Status = No: 7,100 (74.3% of the total)" in facts
    # An average is not additive: no shares.
    avg = SemanticIntent(metric="average_order_value", dimensions=["order_status"])
    assert "of the total" not in _facts([{"order_status": "a", "metric_value": 1.0},
                                          {"order_status": "b", "metric_value": 2.0}], 2, avg, REGISTRY)


def test_explanations_with_numbers_not_in_the_facts_are_rejected():
    facts = "Unique Restaurants by City\n141 row(s).\nCity = New Delhi: 5,473\nHighest: City = New Delhi (5,473). " \
            "Lowest: City = Ghaziabad (1). Across all 141 rows. Average 4.6667"
    assert _grounded("New Delhi has the most restaurants (5,473) of 141 cities.", facts)
    assert _grounded("The average is 4.67, or about 4.7.", facts)
    assert _grounded("The top 5 cities ...", facts)
    assert not _grounded("New Delhi leads with 5,473 and the lowest is 25 in Ghaziabad.", facts)


def test_template_uses_labels_not_internal_names():
    assert _template_explanation(SemanticIntent(metric="total_revenue", dimensions=["customer_state"]), 27, REGISTRY) \
        == "Total Revenue by Customer State: 27 result(s)."
    rows = [{"customer_state": "SP", "metric_value": 10.0}, {"customer_state": "RJ", "metric_value": 4.0}]
    assert _template_explanation(SemanticIntent(metric="total_revenue", dimensions=["customer_state"]), 2, REGISTRY, rows) \
        == ("Total Revenue by Customer State: highest Customer State = SP (10.00), "
            "lowest Customer State = RJ (4.00), across 2 result(s).")
    assert _template_explanation(SemanticIntent(metric="order_count"), 1, REGISTRY, [{"metric_value": 99441}]) \
        == "Number of Orders: 99,441."
