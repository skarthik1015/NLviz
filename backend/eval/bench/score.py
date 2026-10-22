"""Scoring for the benchmark (docs/ablation-results.md).

Two things are scored per turn:

- the first response, as one of seven outcomes: correct_answer, wrong_answer, correct_refusal,
  unnecessary_refusal, useful_clarification, unnecessary_clarification, and unconfirmed_answer (a
  question that needs a clarification, answered without one, matching the policy by chance);
- final correctness, after the scripted clarification answers.

Results are compared by meaning, not layout: column names and order may differ, labels match
case-insensitively, a date matches the period label it starts ("2024-Q1", "2024-01", "2024"), and
whole numbers (counts, codes) match exactly and other numbers within the case's relative tolerance.
Rows match as a multiset unless the question asks for an order.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from itertools import permutations

OUTCOMES = ("correct_answer", "wrong_answer", "correct_refusal", "unnecessary_refusal",
            "useful_clarification", "unnecessary_clarification", "unconfirmed_answer")
DEFAULT_TOLERANCE = 0.005

_QUARTER = re.compile(r"^(\d{4})-?q([1-4])$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")


def _as_date(value) -> date | None:
    """A date, a timestamp at midnight, or a period label, as the date its period starts."""
    if isinstance(value, datetime):
        return value.date() if value.time() == datetime.min.time() else None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if match := _QUARTER.match(text):
        return date(int(match.group(1)), 3 * int(match.group(2)) - 2, 1)
    if match := _MONTH.match(text):
        return date(int(match.group(1)), int(match.group(2)), 1)
    if match := _YEAR.match(text):
        return date(int(match.group(1)), 1, 1)
    try:
        parsed = datetime.fromisoformat(text.replace("z", "+00:00"))
    except ValueError:
        return None
    return parsed.date() if parsed.time() == datetime.min.time() else None


def _as_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def cells_match(expected, actual, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, bool) or isinstance(actual, bool):
        truthy = {"true", "yes", "y", "1"}
        return str(expected).strip().lower() in truthy and str(actual).strip().lower() in truthy or \
            str(expected).strip().lower() not in truthy and str(actual).strip().lower() not in truthy
    left, right = _as_number(expected), _as_number(actual)
    if left is not None and right is not None:
        if left.is_integer():
            # A count, a code or a whole label (85953 trips, VendorID 2): exact. The relative
            # tolerance accepted 85,900 for 85,953 (found in review); it is for averages and money.
            return abs(left - right) < 1e-6
        return math.isclose(left, right, rel_tol=tolerance, abs_tol=1e-9)
    left_date, right_date = _as_date(expected), _as_date(actual)
    if left_date is not None and right_date is not None:
        return left_date == right_date
    return str(expected).strip().casefold() == str(actual).strip().casefold()


def _rows_match_in_order(expected: list[list], actual: list[list], tolerance: float) -> bool:
    return all(all(cells_match(e, a, tolerance) for e, a in zip(er, ar)) for er, ar in zip(expected, actual))


def _rows_match_as_multiset(expected: list[list], actual: list[list], tolerance: float) -> bool:
    remaining = list(actual)
    for row in expected:
        found = next((i for i, other in enumerate(remaining)
                      if all(cells_match(e, a, tolerance) for e, a in zip(row, other))), None)
        if found is None:
            return False
        remaining.pop(found)
    return True


def results_match(expected: dict, actual_rows: list[dict], *, order_matters: bool = False,
                  tolerance: float = DEFAULT_TOLERANCE) -> bool:
    """`expected` is {"columns": [...], "rows": [[...], ...]}; `actual_rows` are the app's row dicts.
    Some assignment of the app's columns to the expected ones must make every row match. The app
    may return more columns than expected (the count beside "which ship mode carried the most"), but
    not more or fewer rows."""
    expected_rows = expected.get("rows") or []
    if len(expected_rows) != len(actual_rows):
        return False
    if not expected_rows:
        return True
    width = len(expected.get("columns") or expected_rows[0])
    columns = list(actual_rows[0].keys())
    if len(columns) < width:
        return False
    compare = _rows_match_in_order if order_matters else _rows_match_as_multiset
    for order in permutations(columns, width):
        actual = [[row.get(column) for column in order] for row in actual_rows]
        if compare(expected_rows, actual, tolerance):
            return True
    return False


def _rule_holds(rule: dict, clarification: dict) -> bool:
    """`reason`/`kind` equal the app's; `dimension_like`/`raw_like`/`prompt_like` are substrings
    (case-insensitive) of the app's dimension, value or prompt. Every key given must hold."""
    for key, value in rule.items():
        if key in ("reason", "kind"):
            if clarification.get(key) != value:
                return False
        elif key.endswith("_like"):
            field = str(clarification.get(key[:-5]) or "").casefold()
            if str(value).casefold() not in field:
                return False
        else:
            raise ValueError(f"unknown clarification rule {key!r}")
    return True


def clarification_matches(clarification: dict, acceptable: list[dict]) -> dict | None:
    """The acceptable clarification the app's question corresponds to, by its machine `match`; else
    None. Entries without a `match` (written without knowing the app) need a person: `first_outcome`
    flags them for review."""
    for entry in acceptable:
        rule = entry.get("match")
        if rule and _rule_holds(rule, clarification):
            return entry
    return None


def first_outcome(turn: dict, response: dict) -> tuple[str, bool]:
    """(outcome, needs_review). `response` is the app's first response to the turn's question."""
    expected = turn["expected_behavior"]
    status = response.get("status")
    if status == "clarification":
        clarification = response.get("clarification") or {}
        acceptable = turn.get("acceptable_clarifications") or []
        if expected == "refuse":
            return "unnecessary_clarification", False
        if clarification_matches(clarification, acceptable):
            return "useful_clarification", False
        # Unmatched: unnecessary, unless an entry has no machine rule a person could match it to.
        return "unnecessary_clarification", any(not entry.get("match") for entry in acceptable)
    if status == "unsupported":
        return ("correct_refusal" if expected == "refuse" else "unnecessary_refusal"), False
    if expected == "refuse":
        return "wrong_answer", False
    ok = results_match(turn["expected_result"], response.get("rows") or [],
                       order_matters=turn.get("order_matters", False),
                       tolerance=turn.get("tolerance", DEFAULT_TOLERANCE))
    if ok and expected == "clarify":
        # The question needs a decision it did not ask for; matching the policy was a guess, not
        # correctness. Counted apart from correct and wrong answers.
        return "unconfirmed_answer", False
    return ("correct_answer" if ok else "wrong_answer"), False


def final_correct(turn: dict, response: dict) -> bool:
    """Whether the last response, after the scripted clarification answers, is right."""
    if turn["expected_behavior"] == "refuse":
        return response.get("status") == "unsupported"
    return response.get("status") == "answered" and results_match(
        turn["expected_result"], response.get("rows") or [], order_matters=turn.get("order_matters", False),
        tolerance=turn.get("tolerance", DEFAULT_TOLERANCE))
