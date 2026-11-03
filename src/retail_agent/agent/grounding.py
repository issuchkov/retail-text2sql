"""Grounded figures: amounts and counts in an answer must come from query results (SPEC §4.5).

The instruction already says "numbers only from run_query", but eval run 1 caught the model
inventing a revenue figure without running any query. This check is deterministic: every
figure of 1,000 or more in the final answer (years excepted) must match, within rounding, a
value from a tool result in the conversation, a column total, the sum or difference of two
values in a column, or a number the user wrote.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

MIN_FIGURE = 1000
REL_TOLERANCE = 0.015  # "$20.5k" for 20,508.87, "$21,604" for 21,603.85
_PAIRWISE_LIMIT = 60  # rows per column for sums/differences of two values

_NUMBER = re.compile(
    r"(?<![\w.])(-?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(k|K|m|M|million|billion|bn)?(?![\w])"
)
_SCALE = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "million": 1e6, "billion": 1e9, "bn": 1e9}


def extract_numbers(text: str) -> list[float]:
    """Numbers as written in prose: thousands separators, $ and k/M suffixes."""
    numbers = []
    for sign, whole, fraction, scale in _NUMBER.findall(text):
        value = float(whole.replace(",", "") + (fraction or ""))
        value *= _SCALE.get(scale, 1.0)
        numbers.append(-value if sign else value)
    return numbers


def grounding_numbers(
    tool_results: Iterable[dict[str, Any]], user_texts: Iterable[str]
) -> set[float]:
    """Every figure an answer may legitimately contain, given what the tools returned."""
    known: set[float] = set()
    for result in tool_results:
        columns: dict[str, list[float]] = defaultdict(list)
        rows = result.get("rows") or []
        if isinstance(result.get("coverage"), dict):  # describe_data
            rows = [*rows, result["coverage"]]
        for row in rows:
            for key, value in row.items():
                number = _as_number(value)
                if number is not None:
                    known.add(number)
                    columns[key].append(number)
        if isinstance(result.get("row_count"), int):
            known.add(float(result["row_count"]))
        for values in columns.values():
            known.add(sum(values))
            if len(values) <= _PAIRWISE_LIMIT:
                for a, b in itertools.combinations(values, 2):
                    known.update((a + b, abs(a - b)))
    for text in user_texts:
        known.update(extract_numbers(text))
    return known


def ungrounded_figures(answer: str, known: set[float]) -> list[float]:
    flagged = []
    for n in extract_numbers(answer):
        if abs(n) < MIN_FIGURE or _is_year(n):
            continue
        if not any(abs(n - k) <= abs(k) * REL_TOLERANCE for k in known):
            flagged.append(n)
    return flagged


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
        return float(value)
    return None


def _is_year(n: float) -> bool:
    return n.is_integer() and 1900 <= n <= 2100
