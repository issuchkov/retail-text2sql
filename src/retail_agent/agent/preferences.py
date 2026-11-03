"""Answer-format preferences per user (SPEC §3.5, R4 user level).

Saved only when the user states them; added to the instruction of every later turn and
conversation. They change presentation only — never scope, definitions or safety rules.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

ASPECTS: dict[str, tuple[str, ...]] = {
    "format": ("tables", "bullets", "prose"),
    "depth": ("brief", "standard", "detailed"),
    "visuals": ("text", "charts"),
}

_SYNONYMS = {
    "format": {
        "table": "tables",
        "tabular": "tables",
        "bullet": "bullets",
        "bullet points": "bullets",
        "bullet point": "bullets",
        "list": "bullets",
        "lists": "bullets",
        "text": "prose",
        "paragraphs": "prose",
    },
    "depth": {
        "short": "brief",
        "concise": "brief",
        "high level": "brief",
        "high-level": "brief",
        "normal": "standard",
        "medium": "standard",
        "deep": "detailed",
        "deep dive": "detailed",
        "in depth": "detailed",
        "in-depth": "detailed",
        "thorough": "detailed",
    },
    "visuals": {"chart": "charts", "graphs": "charts", "graph": "charts", "no charts": "text"},
}

GUIDANCE = {
    ("format", "tables"): "present figures in Markdown tables",
    ("format", "bullets"): "present figures as bullet points, not tables",
    ("format", "prose"): "present figures in short paragraphs, without tables or bullets",
    ("depth", "brief"): "keep answers to the headline numbers and one or two sentences of "
    "interpretation",
    ("depth", "standard"): "give the numbers, the main driver and one next step",
    ("depth", "detailed"): "include breakdowns, likely drivers, caveats and next steps",
    ("visuals", "text"): "do not suggest charts",
    ("visuals", "charts"): "add a chart-ready table and name the chart that fits (bar, line, "
    "pie); you cannot draw charts yourself",
}

DEFAULT = "default"


def normalize_preference(aspect: str, value: str) -> tuple[str, str] | None:
    key = " ".join(aspect.lower().split())
    if key not in ASPECTS:
        return None
    cleaned = " ".join(value.lower().split())
    if cleaned == DEFAULT:
        return key, DEFAULT
    cleaned = _SYNONYMS[key].get(cleaned, cleaned)
    return (key, cleaned) if cleaned in ASPECTS[key] else None


class PreferenceStore:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS user_preferences (user_id TEXT NOT NULL, "
            "aspect TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY (user_id, aspect))"
        )

    def all(self, user_id: str) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT aspect, value FROM user_preferences WHERE user_id = ? ORDER BY aspect",
            (user_id,),
        ).fetchall()
        return dict(rows)

    def set(self, user_id: str, aspect: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO user_preferences (user_id, aspect, value, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (user_id, aspect) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (user_id, aspect, value, dt.datetime.now(dt.UTC).isoformat()),
            )

    def delete(self, user_id: str, aspect: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM user_preferences WHERE user_id = ? AND aspect = ?", (user_id, aspect)
            )


def instruction_lines(saved: dict[str, str]) -> str:
    if not saved:
        return "No saved preferences: use Markdown tables for figures and a standard depth."
    return "\n".join(
        f"- {aspect}: {value} — {GUIDANCE[(aspect, value)]}" for aspect, value in saved.items()
    )
