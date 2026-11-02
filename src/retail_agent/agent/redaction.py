"""Output guard: redact PII patterns from text (SPEC §4.5).

PII columns never reach the model (the query layer removes them), so this is a second
line of defence against personal data typed by the user or invented by the model ending
up in an answer, a saved report or a trace.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

_STREET_SUFFIX = (
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Lane|Ln\.?|Drive|Dr\.?|"
    r"Court|Ct\.?|Way|Parkway|Pkwy\.?|Place|Pl\.?|Terrace|Circle)"
)

PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "coordinates": re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}"),
    "phone": re.compile(
        r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"
    ),
    "street_address": re.compile(rf"\b\d{{1,6}}\s+(?:[A-Z][A-Za-z]+\s+){{1,3}}{_STREET_SUFFIX}\b"),
}


def redact(text: str) -> tuple[str, list[str]]:
    """Return the text with PII patterns replaced, and the kinds that were found."""
    kinds = []
    for kind, pattern in PATTERNS.items():
        text, count = pattern.subn(REDACTED, text)
        if count:
            kinds.append(kind)
    return text, kinds


def redact_value(value: Any) -> tuple[Any, list[str]]:
    """Redact every string inside nested dicts and lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out, kinds = {}, []
        for k, v in value.items():
            out[k], found = redact_value(v)
            kinds.extend(found)
        return out, _unique(kinds)
    if isinstance(value, list):
        items, kinds = [], []
        for v in value:
            cleaned, found = redact_value(v)
            items.append(cleaned)
            kinds.extend(found)
        return items, _unique(kinds)
    return value, []


def _unique(kinds: list[str]) -> list[str]:
    return list(dict.fromkeys(kinds))
