"""Business definitions with overrides (SPEC §3.4, §8 R4).

Precedence: an override for this conversation (session state) → the user's saved
definition (SQLite, only on an explicit request to remember) → the default. Every answer
names the source, so a changed number is never a surprise.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SESSION_KEY = "definitions"

DEFAULTS = {
    "revenue": "sum of sale_price of items whose status is not Cancelled or Returned "
    "(orders.order_revenue is already net)",
    "churn": "no purchase in the 90 days before the end of the period analysed; count only "
    "customers with at least one item that was not cancelled or returned, judged by their "
    "last such purchase",
    "quarter": "calendar quarter in UTC (months and weeks are calendar periods in UTC too)",
}

_ALIASES = {
    "churned customer": "churn",
    "churned customers": "churn",
    "churn rate": "churn",
    "net revenue": "revenue",
    "sales": "revenue",
    "quarters": "quarter",
    "fiscal quarter": "quarter",
}

SOURCE_DEFAULT = "default"
SOURCE_CONVERSATION = "this conversation"
SOURCE_SAVED = "your saved definition"


def normalize_term(term: str) -> str:
    cleaned = " ".join(term.lower().split())
    return _ALIASES.get(cleaned, cleaned)


class DefinitionStore:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS user_definitions (user_id TEXT NOT NULL, "
            "term TEXT NOT NULL, definition TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "PRIMARY KEY (user_id, term))"
        )

    def all(self, user_id: str) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT term, definition FROM user_definitions WHERE user_id = ? ORDER BY term",
            (user_id,),
        ).fetchall()
        return dict(rows)

    def set(self, user_id: str, term: str, definition: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO user_definitions (user_id, term, definition, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (user_id, term) DO UPDATE SET "
                "definition = excluded.definition, updated_at = excluded.updated_at",
                (user_id, term, definition, dt.datetime.now(dt.UTC).isoformat()),
            )

    def delete(self, user_id: str, term: str) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM user_definitions WHERE user_id = ? AND term = ?", (user_id, term)
            )


def effective(
    state: Mapping[str, Any], store: DefinitionStore, user_id: str
) -> list[tuple[str, str, str]]:
    """(term, definition, source) for every known term, defaults first."""
    saved = store.all(user_id)
    session = state.get(SESSION_KEY) or {}
    terms = list(DEFAULTS) + sorted((set(saved) | set(session)) - set(DEFAULTS))
    result = []
    for term in terms:
        if term in session:
            result.append((term, session[term], SOURCE_CONVERSATION))
        elif term in saved:
            result.append((term, saved[term], SOURCE_SAVED))
        else:
            result.append((term, DEFAULTS[term], SOURCE_DEFAULT))
    return result
