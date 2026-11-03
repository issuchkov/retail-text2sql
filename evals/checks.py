"""Deterministic answer checks for the eval harness. No model judges anything here."""

from __future__ import annotations

from retail_agent.agent.grounding import extract_numbers
from retail_agent.agent.redaction import PATTERNS


def contains_value(text: str, value: float, *, rel: float = 0.015, abs_tol: float = 0.06) -> bool:
    """Is `value` written in `text`, allowing for rounding (85% for 84.6, $20.5k for 20,508)?"""
    tolerance = max(abs(value) * rel, abs_tol)
    return any(abs(n - value) <= tolerance for n in extract_numbers(text))


def _normalize(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'").casefold()


def contains_text(text: str, needle: str) -> bool:
    return _normalize(needle) in _normalize(text)


def pii_findings(text: str) -> list[str]:
    return [kind for kind, pattern in PATTERNS.items() if pattern.search(text)]
