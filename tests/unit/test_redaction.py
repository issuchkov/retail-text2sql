"""Output guard: PII patterns are redacted from answers and reports (SPEC §4.5, AC-2)."""

import pytest

from retail_agent.agent.redaction import redact, redact_value


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("Contact jane.doe@example.com for details", "email"),
        ("Call +1 (415) 555-0134 today", "phone"),
        ("Call 415-555-0134 today", "phone"),
        ("Located at 40.712776, -74.005974", "coordinates"),
        ("Ships to 1600 Amphitheatre Parkway in town", "street_address"),
        ("Lives at 12 Baker Street", "street_address"),
    ],
)
def test_redacts_pii_patterns(raw: str, kind: str) -> None:
    cleaned, kinds = redact(raw)
    assert "[redacted]" in cleaned
    assert kind in kinds


@pytest.mark.parametrize(
    "safe",
    [
        "Revenue grew 12.5% to $1,234,567.89 in Q3 2026",
        "Top brand: Levi's with 1,573 items; customer_key 3fa2b9c41d0e7a55",
        "Orders 2026-07-01 to 2026-09-30, average order value 84.20",
        "California, Texas and New York lead; age band 25-34",
        "SELECT brand, SUM(order_revenue) FROM orders GROUP BY 1 LIMIT 1000",
    ],
)
def test_keeps_ordinary_analysis_text(safe: str) -> None:
    assert redact(safe) == (safe, [])


def test_redacts_nested_values() -> None:
    value = {"rows": [{"note": "mail a@b.co"}, {"n": 3}], "title": "ok"}
    cleaned, kinds = redact_value(value)
    assert cleaned == {"rows": [{"note": "mail [redacted]"}, {"n": 3}], "title": "ok"}
    assert kinds == ["email"]
