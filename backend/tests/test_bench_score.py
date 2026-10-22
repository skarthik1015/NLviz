"""The benchmark scorer compares meaning, not layout, and assigns the six first-response outcomes."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval" / "bench"))

from score import cells_match, final_correct, first_outcome, results_match  # noqa: E402

QUARTERS = {"columns": ["quarter", "value"], "rows": [["2024-Q1", 1234.5], ["2024-Q2", 987.0]]}


def test_columns_names_and_order_do_not_matter():
    rows = [{"metric_value": 987.0, "processed_quarter": "2024-04-01T00:00:00"},
            {"metric_value": 1234.5, "processed_quarter": "2024-01-01"}]
    assert results_match(QUARTERS, rows)


def test_order_counts_only_when_asked():
    rows = [{"q": "2024-04-01", "v": 987.0}, {"q": "2024-01-01", "v": 1234.5}]
    assert results_match(QUARTERS, rows) and not results_match(QUARTERS, rows, order_matters=True)


def test_whole_numbers_match_exactly_and_others_within_the_tolerance():
    assert cells_match(100.5, 100.9) and not cells_match(100.5, 101.6)
    assert not cells_match(85953, 85900) and not cells_match(100.0, 100.4) and cells_match(2, "2")
    assert cells_match(0.0, 0.0) and not cells_match(0.0, 0.001)


def test_labels_match_case_insensitively_and_periods_match_their_start_date():
    assert cells_match("Brazil", "BRAZIL ") and not cells_match("Brazil", "Brasil")
    assert cells_match("2024-03", "2024-03-01T00:00:00") and cells_match("2024", "2024-01-01")
    assert not cells_match("2024-Q2", "2024-01-01")
    assert cells_match(2024, "2024")  # a year as a number or a label


def test_a_missing_row_or_column_fails_but_an_extra_column_is_allowed():
    assert not results_match(QUARTERS, [{"q": "2024-01-01", "v": 1234.5}])
    assert not results_match(QUARTERS, [{"q": "2024-01-01"}, {"q": "2024-04-01"}])
    label_only = {"columns": ["mode"], "rows": [["TRUCK"]]}
    assert results_match(label_only, [{"ship_mode": "TRUCK", "metric_value": 86000}])


def test_clarification_rules_match_reason_and_substrings():
    from score import clarification_matches

    clarification = {"kind": "value", "reason": "no_match", "dimension": "payment_type", "raw": "Cash"}
    hit = {"about": "which payment code", "match": {"reason": "no_match", "dimension_like": "payment"}}
    miss = {"about": "zeros", "match": {"reason": "zero_placeholder"}}
    assert clarification_matches(clarification, [miss, hit]) is hit
    assert clarification_matches(clarification, [miss]) is None


def _turn(behavior, **extra):
    return {"expected_behavior": behavior, "expected_result": QUARTERS, **extra}


ANSWER = {"status": "answered", "rows": [{"q": "2024-01-01", "v": 1234.5}, {"q": "2024-04-01", "v": 987.0}]}


def test_six_outcomes():
    status_clarify = {"status": "clarification", "clarification": {"reason": "zero_placeholder"}}
    acceptable = [{"about": "zeros", "match": {"reason": "zero_placeholder"}}]
    assert first_outcome(_turn("answer"), ANSWER) == ("correct_answer", False)
    assert first_outcome(_turn("answer"), {"status": "answered", "rows": []}) == ("wrong_answer", False)
    assert first_outcome(_turn("refuse"), {"status": "unsupported"}) == ("correct_refusal", False)
    assert first_outcome(_turn("answer"), {"status": "unsupported"}) == ("unnecessary_refusal", False)
    assert first_outcome(_turn("clarify", acceptable_clarifications=acceptable), status_clarify) == (
        "useful_clarification", False)
    assert first_outcome(_turn("answer"), status_clarify) == ("unnecessary_clarification", False)
    assert first_outcome(_turn("refuse"), ANSWER) == ("wrong_answer", False)


def test_a_clarification_without_a_machine_rule_is_flagged_for_a_person():
    turn = _turn("clarify", acceptable_clarifications=[{"about": "which status counts"}])
    assert first_outcome(turn, {"status": "clarification", "clarification": {"reason": "no_match"}}) == (
        "unnecessary_clarification", True)


def test_final_correctness_is_scored_after_the_clarifications():
    assert final_correct(_turn("clarify"), ANSWER)
    assert final_correct(_turn("refuse"), {"status": "unsupported"})
    assert not final_correct(_turn("answer"), {"status": "clarification"})


def test_a_guess_where_a_clarification_was_required_is_not_correct():
    assert first_outcome(_turn("clarify"), ANSWER) == ("unconfirmed_answer", False)
