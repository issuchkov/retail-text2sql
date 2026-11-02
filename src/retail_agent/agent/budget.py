"""Per-turn budgets: model calls, queries and failed query attempts (SPEC §6).

Counters live in session state under a `temp:` key (never persisted) and are tied to the
invocation id, so a new turn starts from zero whatever the session service keeps in
memory. A delete confirmation resumes the same invocation and keeps its counters.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

KEY = "temp:turn_budget"


def counters(state: MutableMapping[str, Any], invocation_id: str) -> dict[str, Any]:
    current = state.get(KEY)
    if not isinstance(current, dict) or current.get("invocation") != invocation_id:
        return {"invocation": invocation_id, "model_calls": 0, "queries": 0, "failed": 0}
    return dict(current)


def save(state: MutableMapping[str, Any], values: dict[str, Any]) -> None:
    state[KEY] = values
