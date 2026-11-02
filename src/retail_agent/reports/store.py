"""Saved-report library (SPEC §5.1, §5.3). SQLite in the prototype.

Every read and delete is owner-scoped. Deletes are permanent; the audit log keeps who did
what to which report ids, never report content.
"""

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
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports (owner_id);
CREATE TABLE IF NOT EXISTS report_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    report_ids TEXT NOT NULL,
    trace_id TEXT NOT NULL
);
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


@dataclass(frozen=True)
class AuditEntry:
    at: dt.datetime
    user_id: str
    action: str
    report_ids: list[int]
    trace_id: str


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
            "SELECT * FROM reports WHERE id = ? AND owner_id = ?",
            (report_id, owner_id),
        ).fetchone()
        return _to_report(row) if row else None

    def list(self, owner_id: str) -> list[Report]:
        rows = self._db.execute(
            "SELECT * FROM reports WHERE owner_id = ? ORDER BY id",
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

    def delete(self, owner_id: str, ids: list[int]) -> list[int]:
        """Permanently delete those of `ids` that exist and belong to `owner_id`."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._db:
            rows = self._db.execute(
                f"SELECT id FROM reports WHERE owner_id = ? AND id IN ({marks}) ORDER BY id",
                (owner_id, *ids),
            ).fetchall()
            deleted = [r["id"] for r in rows]
            if deleted:
                marks = ",".join("?" * len(deleted))
                self._db.execute(f"DELETE FROM reports WHERE id IN ({marks})", deleted)
        return deleted

    def audit(self, user_id: str, action: str, report_ids: list[int], trace_id: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO report_audit (at, user_id, action, report_ids, trace_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._clock().isoformat(), user_id, action, json.dumps(report_ids), trace_id),
            )

    def audit_log(self, user_id: str) -> list[AuditEntry]:
        rows = self._db.execute(
            "SELECT * FROM report_audit WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
        return [
            AuditEntry(
                at=dt.datetime.fromisoformat(r["at"]),
                user_id=r["user_id"],
                action=r["action"],
                report_ids=json.loads(r["report_ids"]),
                trace_id=r["trace_id"],
            )
            for r in rows
        ]


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
    )
