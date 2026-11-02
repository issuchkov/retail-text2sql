"""Two-phase deletion of saved reports (SPEC §5.3).

`plan_deletion` resolves a selector to the user's own reports and produces the preview the
user must confirm. `execute_deletion` deletes exactly the previewed ids, and only when the
user typed the confirmation phrase. The check lives here, not only in the CLI, so no other
caller can delete without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from retail_agent.reports.store import ReportStore, Selector

MAX_BULK = 20


def confirmation_phrase(count: int) -> str:
    return f"delete {count}"


def phrase_matches(typed: str, count: int) -> bool:
    return " ".join(typed.split()).casefold() == confirmation_phrase(count)


@dataclass(frozen=True)
class DeletionPlan:
    status: Literal["needs_confirmation", "nothing_matched", "too_many"]
    ids: list[int] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    matched: int = 0

    @property
    def phrase(self) -> str:
        return confirmation_phrase(len(self.ids))


@dataclass(frozen=True)
class DeletionOutcome:
    status: Literal["deleted", "cancelled"]
    ids: list[int] = field(default_factory=list)


def plan_deletion(
    store: ReportStore, owner_id: str, selector: Selector, conversation_id: str
) -> DeletionPlan:
    reports = store.resolve(owner_id, selector, conversation_id)
    if not reports:
        return DeletionPlan("nothing_matched")
    if len(reports) > MAX_BULK:
        return DeletionPlan("too_many", matched=len(reports))
    items = [
        {"id": r.id, "title": r.title, "created": r.created_at.date().isoformat()} for r in reports
    ]
    return DeletionPlan(
        "needs_confirmation", ids=[r.id for r in reports], items=items, matched=len(reports)
    )


def execute_deletion(
    store: ReportStore, owner_id: str, planned_ids: list[int], *, typed: str
) -> DeletionOutcome:
    if not planned_ids or not phrase_matches(typed, len(planned_ids)):
        return DeletionOutcome("cancelled")
    return DeletionOutcome("deleted", ids=store.soft_delete(owner_id, planned_ids))
