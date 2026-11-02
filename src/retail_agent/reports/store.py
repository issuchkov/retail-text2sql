"""Saved-report library (SPEC §5.1). SQLite in the prototype; every read is owner-scoped."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, model_validator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    entities TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports (owner_id, deleted_at);
"""


@dataclass(frozen=True)
class Report:
    id: int
    owner_id: str
    conversation_id: str
    title: str
    body: str
    entities: list[str]
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None


class Selector(BaseModel):
    """Which of the user's reports an operation targets. Set criteria are combined with AND."""

    ids: list[int] | None = None
    mentions: str | None = None
    this_conversation: bool = False
    older_than: dt.date | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Selector:
        if not (
            self.ids
            or (self.mentions and self.mentions.strip())
            or self.this_conversation
            or self.older_than
        ):
            raise ValueError("selector needs ids, mentions, this_conversation or older_than")
        return self


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ReportStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], dt.datetime] = _utcnow) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._clock = clock

    def save(
        self,
        owner_id: str,
        conversation_id: str,
        *,
        title: str,
        body: str,
        entities: list[str] | None = None,
    ) -> Report:
        now = self._clock().isoformat()
        with self._db:
            cur = self._db.execute(
                "INSERT INTO reports (owner_id, conversation_id, title, body, entities, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (owner_id, conversation_id, title, body, json.dumps(entities or []), now, now),
            )
        report = self.get(owner_id, cur.lastrowid)
        assert report is not None
        return report

    def get(self, owner_id: str, report_id: int) -> Report | None:
        row = self._db.execute(
            "SELECT * FROM reports WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
            (report_id, owner_id),
        ).fetchone()
        return _to_report(row) if row else None

    def list(self, owner_id: str) -> list[Report]:
        rows = self._db.execute(
            "SELECT * FROM reports WHERE owner_id = ? AND deleted_at IS NULL ORDER BY id",
            (owner_id,),
        ).fetchall()
        return [_to_report(r) for r in rows]

    def resolve(self, owner_id: str, selector: Selector, conversation_id: str) -> list[Report]:
        reports = self.list(owner_id)
        if selector.ids:
            wanted = set(selector.ids)
            reports = [r for r in reports if r.id in wanted]
        if selector.mentions and selector.mentions.strip():
            needle = selector.mentions.strip().casefold()
            reports = [r for r in reports if needle in _searchable(r)]
        if selector.this_conversation:
            reports = [r for r in reports if r.conversation_id == conversation_id]
        if selector.older_than:
            reports = [r for r in reports if r.created_at.date() < selector.older_than]
        return reports

    def soft_delete(self, owner_id: str, ids: list[int]) -> list[int]:
        return self._set_deleted(owner_id, ids, deleted=True)

    def restore(self, owner_id: str, ids: list[int]) -> list[int]:
        return self._set_deleted(owner_id, ids, deleted=False)

    def _set_deleted(self, owner_id: str, ids: list[int], *, deleted: bool) -> list[int]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        state = "deleted_at IS NULL" if deleted else "deleted_at IS NOT NULL"
        with self._db:
            rows = self._db.execute(
                f"SELECT id FROM reports WHERE owner_id = ? AND {state} AND id IN ({marks}) "
                "ORDER BY id",
                (owner_id, *ids),
            ).fetchall()
            affected = [r["id"] for r in rows]
            if affected:
                stamp = self._clock().isoformat() if deleted else None
                marks = ",".join("?" * len(affected))
                self._db.execute(
                    f"UPDATE reports SET deleted_at = ? WHERE id IN ({marks})",
                    (stamp, *affected),
                )
        return affected


def _searchable(report: Report) -> str:
    return "\n".join([report.title, report.body, *report.entities]).casefold()


def _to_report(row: sqlite3.Row) -> Report:
    return Report(
        id=row["id"],
        owner_id=row["owner_id"],
        conversation_id=row["conversation_id"],
        title=row["title"],
        body=row["body"],
        entities=json.loads(row["entities"]),
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        updated_at=dt.datetime.fromisoformat(row["updated_at"]),
        deleted_at=dt.datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
    )
