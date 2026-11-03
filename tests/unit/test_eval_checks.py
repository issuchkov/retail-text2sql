"""Eval checks are deterministic code, tested like any other (PLAN stage 9)."""

import pytest

from evals.checks import contains_text, contains_value, extract_numbers, pii_findings


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Revenue was $20,508.87 in Q3", [20508.87]),  # "Q3" is not a number
        ("84.6% churned", [84.6]),
        ("about 20.5k and 1.2M", [20500.0, 1_200_000.0]),
        ("1.2 million customers", [1_200_000.0]),
        ("-3.5% vs last year", [-3.5]),
        ("2,629 of 3,107", [2629.0, 3107.0]),
    ],
)
def test_extract_numbers(text: str, expected: list[float]) -> None:
    assert extract_numbers(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "value", "found"),
    [
        ("Diesel: $20,508.87", 20508.87, True),
        ("Diesel: $20,509", 20508.87, True),
        ("Diesel: $20.5k", 20508.87, True),
        ("Diesel: $19,800", 20508.87, False),
        ("Churn rate 85%", 84.6, True),
        ("Churn rate 80%", 84.6, False),
        ("2,629 churned", 2629, True),
        ("no numbers here", 12.0, False),
    ],
)
def test_contains_value(text: str, value: float, found: bool) -> None:
    assert contains_value(text, value) is found


def test_contains_value_strict_tolerance() -> None:
    assert not contains_value("Calvin Klein made $151,700", 151724.0, rel=0.0001)
    assert contains_value("Calvin Klein made $151,724.00", 151724.0, rel=0.0001)


def test_contains_text_normalizes_case_and_quotes() -> None:
    assert contains_text("LEVI\u2019S leads", "Levi's")
    assert not contains_text("Wrangler leads", "Levi's")


def test_pii_findings() -> None:
    assert pii_findings("mail jane@corp.com or 415-555-0134") == ["email", "phone"]
    assert pii_findings("Revenue $1,234 in 2026-07") == []
