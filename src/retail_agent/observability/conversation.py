"""Deep dive: the full message correspondence of a turn (SPEC §7).

Traces hold metadata; the conversation itself — user and model messages, tool calls with
arguments and results — lives in the ADK session store. This renders it, for one trace id
(the invocation id in the CLI footer) or for a whole session.

    uv run python scripts/show_conversation.py <trace_id | session_id>
"""

from __future__ import annotations

import json
from typing import Any

MAX_ROWS = 5


def render_turns(events: list[Any], trace_id: str | None = None) -> str:
    lines: list[str] = []
    current = None
    for event in events:
        if trace_id and event.invocation_id != trace_id:
            continue
        if event.invocation_id != current:
            current = event.invocation_id
            lines.append(f"\n=== trace {current}")
        for part in (event.content.parts if event.content else None) or []:
            if part.text:
                lines.append(f"[{event.author}] {part.text.strip()}")
            if part.function_call:
                lines.append(f"[{event.author}] → {part.function_call.name}")
                lines.extend(_indent(_args(part.function_call.args or {})))
            if part.function_response:
                lines.append(f"[tool] ← {part.function_response.name}")
                lines.extend(_indent(_result(part.function_response.response or {})))
    return "\n".join(lines).strip()


def _args(args: dict[str, Any]) -> str:
    if "sql" in args:
        rest = {k: v for k, v in args.items() if k != "sql"}
        return (json.dumps(rest, ensure_ascii=False) + "\n" if rest else "") + str(args["sql"])
    return json.dumps(args, ensure_ascii=False, default=str)


def _result(result: dict[str, Any]) -> str:
    shown = dict(result)
    rows = shown.get("rows")
    if isinstance(rows, list) and len(rows) > MAX_ROWS:
        shown["rows"] = [*rows[:MAX_ROWS], f"... {len(rows) - MAX_ROWS} more rows"]
    return json.dumps(shown, ensure_ascii=False, default=str, indent=1)


def _indent(text: str) -> list[str]:
    return ["    " + line for line in text.splitlines()]
