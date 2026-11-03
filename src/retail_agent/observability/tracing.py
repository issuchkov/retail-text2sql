"""Per-turn traces as JSON lines (SPEC §7).

`TracePlugin` is an ADK plugin: it sees every user message, model call and tool call of
an invocation and writes one span per step plus a `turn` summary, all keyed by the
invocation id (the trace id shown in the CLI). Spans carry metadata only — model names,
tokens, latency, SQL, guard decisions, row counts, bytes — never answer text or result
rows; every string is passed through the PII redactor before it is written.

Production: the same spans map onto OpenTelemetry (ADK emits OTel spans natively) and go
to Cloud Trace, with the JSON lines shipped to Cloud Logging and a BigQuery sink.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from retail_agent.agent.redaction import redact_value

REDACTIONS_KEY = "temp:redactions"  # set by the agent's output guard
UNGROUNDED_KEY = "temp:ungrounded"  # figures in the answer that no query produced
QUERY_TOOLS = {"run_query", "list_column_values", "describe_data"}


class TraceWriter:
    """Appends spans to a JSON-lines file and reads them back for /trace and /stats."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._finalized: set[str] = set()
        self.active: dict[str, str] = {}  # session id -> trace id of the running turn

    def write(self, span: dict[str, Any]) -> None:
        record, _ = redact_value({"ts": dt.datetime.now(dt.UTC).isoformat(), **span})
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if span.get("kind") == "turn":
            self._finalized.add(str(span.get("trace_id")))

    def finalized(self, trace_id: str) -> bool:
        return trace_id in self._finalized

    def read(self, *, session_id: str | None = None, trace_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        spans = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is not None and span.get("session_id") != session_id:
                continue
            if trace_id is not None and span.get("trace_id") != trace_id:
                continue
            spans.append(span)
        return spans


@dataclass
class _Turn:
    trace_id: str
    session_id: str
    user_id: str
    started: float
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    queries: int = 0
    bytes_processed: int = 0
    statuses: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)
    ungrounded: list[float] = field(default_factory=list)
    model_started: float | None = None
    tool_started: dict[str, float] = field(default_factory=dict)


class TracePlugin(BasePlugin):
    def __init__(self, writer: TraceWriter, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(name="tracing")
        self._writer = writer
        self._clock = clock
        self._turns: dict[str, _Turn] = {}

    # --- turn ---------------------------------------------------------------------------

    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content
    ) -> types.Content | None:
        turn = self._turn(invocation_context)
        parts = user_message.parts or []
        confirmations = [p.function_response for p in parts if p.function_response]
        span: dict[str, Any] = {"kind": "user_message"}
        if confirmations:
            span["confirmation_reply"] = True
        else:
            message = " ".join(p.text for p in parts if p.text)
            span.update(chars=len(message), text=message[:2000])
        self._emit(turn, span)
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        turn = self._turns.pop(invocation_context.invocation_id, None)
        if turn is None:
            return
        state = invocation_context.session.state
        for kind in state.get(REDACTIONS_KEY) or []:
            if kind not in turn.redactions:
                turn.redactions.append(kind)
        turn.ungrounded = list(state.get(UNGROUNDED_KEY) or [])
        if "awaiting_confirmation" in turn.statuses:
            status = "awaiting_confirmation"
        elif "budget_exhausted" in turn.statuses:
            status = "budget_exhausted"
        else:
            status = "ok"
        self._finish(turn, status)

    async def on_run_error_callback(
        self, *, invocation_context: InvocationContext, error: Exception
    ) -> None:
        turn = self._turns.pop(invocation_context.invocation_id, None)
        if turn is not None:
            self._finish(turn, "error", error=type(error).__name__)

    # --- model --------------------------------------------------------------------------

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        turn = self._turns.get(callback_context.invocation_id)
        if turn is not None:
            turn.model_started = self._clock()
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        turn = self._turns.get(callback_context.invocation_id)
        if turn is None or llm_response.partial:
            return None
        meta = llm_response.custom_metadata or {}
        usage = llm_response.usage_metadata
        tokens_in = (usage.prompt_token_count or 0) if usage else 0
        tokens_out = (usage.candidates_token_count or 0) if usage else 0
        turn.model_calls += 1
        turn.tokens_in += tokens_in
        turn.tokens_out += tokens_out
        parts = llm_response.content.parts if llm_response.content else None
        self._emit(
            turn,
            {
                "kind": "model_call",
                "model": meta.get("model") or llm_response.model_version,
                "attempts": meta.get("attempts", []),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "finish_reason": llm_response.finish_reason.name
                if llm_response.finish_reason
                else None,
                "function_calls": [p.function_call.name for p in parts or [] if p.function_call],
                "duration_ms": self._since(turn.model_started),
            },
        )
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> LlmResponse | None:
        turn = self._turns.get(callback_context.invocation_id)
        if turn is not None:
            self._emit(
                turn,
                {
                    "kind": "model_error",
                    "error": type(error).__name__,
                    "detail": str(error)[:300],
                    "duration_ms": self._since(turn.model_started),
                },
            )
        return None

    # --- tools --------------------------------------------------------------------------

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict | None:
        turn = self._turns.get(tool_context.invocation_id)
        if turn is not None:
            turn.tool_started[tool_context.function_call_id or ""] = self._clock()
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        turn = self._turns.get(tool_context.invocation_id)
        if turn is None:
            return None
        started = turn.tool_started.pop(tool_context.function_call_id or "", None)
        status = str(result.get("status", "ok")) if isinstance(result, dict) else "ok"
        turn.statuses.append(status)
        span: dict[str, Any] = {
            "kind": "tool_call",
            "tool": tool.name,
            "status": status,
            "duration_ms": self._since(started),
            **_args_summary(tool.name, tool_args),
        }
        if isinstance(result, dict):
            span.update(_result_summary(result))
            if tool.name in QUERY_TOOLS and (status == "ok" or "kind" in result):
                turn.queries += 1
                turn.bytes_processed += int(result.get("bytes_processed") or 0)
            for kind in result.get("redacted") or []:
                if kind not in turn.redactions:
                    turn.redactions.append(kind)
        self._emit(turn, span)
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        turn = self._turns.get(tool_context.invocation_id)
        if turn is not None:
            started = turn.tool_started.pop(tool_context.function_call_id or "", None)
            self._emit(
                turn,
                {
                    "kind": "tool_error",
                    "tool": tool.name,
                    "error": type(error).__name__,
                    "duration_ms": self._since(started),
                },
            )
        return None

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Event | None:
        return None

    # --- helpers ------------------------------------------------------------------------

    def _turn(self, invocation_context: InvocationContext) -> _Turn:
        session = invocation_context.session
        turn = _Turn(
            trace_id=invocation_context.invocation_id,
            session_id=session.id,
            user_id=session.user_id,
            started=self._clock(),
        )
        self._turns[turn.trace_id] = turn
        self._writer.active[session.id] = turn.trace_id
        return turn

    def _emit(self, turn: _Turn, span: dict[str, Any]) -> None:
        self._writer.write(
            {
                "trace_id": turn.trace_id,
                "session_id": turn.session_id,
                "at_ms": self._since(turn.started),
                **span,
            }
        )

    def _finish(self, turn: _Turn, status: str, error: str | None = None) -> None:
        span: dict[str, Any] = {
            "kind": "turn",
            "user_id": turn.user_id,
            "status": status,
            "duration_ms": self._since(turn.started),
            "model_calls": turn.model_calls,
            "tokens_in": turn.tokens_in,
            "tokens_out": turn.tokens_out,
            "queries": turn.queries,
            "bytes_processed": turn.bytes_processed,
            "redactions": turn.redactions,
            "ungrounded": turn.ungrounded,
        }
        if error:
            span["error"] = error
        self._emit(turn, span)

    def _since(self, started: float | None) -> int | None:
        return None if started is None else int((self._clock() - started) * 1000)


def write_failed_turn(
    writer: TraceWriter, *, trace_id: str, session_id: str, user_id: str, error: Exception
) -> None:
    """Close a turn the plugin could not close itself (an exception escaped the runner)."""
    if writer.finalized(trace_id):
        return
    writer.write(
        {
            "trace_id": trace_id,
            "session_id": session_id,
            "kind": "turn",
            "user_id": user_id,
            "status": "error",
            "error": type(error).__name__,
            "duration_ms": None,
            "model_calls": 0,
            "queries": 0,
            "bytes_processed": 0,
            "redactions": [],
        }
    )


def _args_summary(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool == "run_query":
        return {"sql": str(args.get("sql", ""))[:4000], "purpose": args.get("purpose", "")}
    if tool == "list_column_values":
        return {"table": args.get("table"), "column": args.get("column")}
    if tool == "delete_reports":
        return {"selector": {k: v for k, v in args.items() if v not in (None, False, [])}}
    if tool == "save_report":  # report content is not traced
        return {"body_chars": len(str(args.get("body", ""))), "entities": args.get("entities")}
    return {}


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("rule", "row_count", "bytes_processed", "truncated", "count", "matched"):
        if key in result:
            summary[key] = result[key]
    if "kind" in result:
        summary["error_kind"] = result["kind"]
    if "deleted_ids" in result:
        summary["deleted"] = len(result["deleted_ids"])
    if "redacted" in result:
        summary["redacted"] = result["redacted"]
    if "examples" in result:  # find_similar_analyses
        summary["method"] = result.get("method")
        summary["examples"] = [e.get("id") for e in result["examples"]]
    return summary
