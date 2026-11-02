"""Traces, metrics and the /trace, /stats, /feedback commands (SPEC §7, AC-2, AC-8)."""

from __future__ import annotations

import json

import pytest
from google.adk.sessions import InMemorySessionService
from rich.console import Console

from retail_agent.cli.app import App
from retail_agent.cli.chat import ChatSession
from retail_agent.observability.metrics import percentile, summarize
from retail_agent.observability.tracing import TraceWriter
from retail_agent.query.executor import QueryFailed
from tests.unit.fakes import ScriptedLlm, call, text
from tests.unit.test_cli import services as services


@pytest.fixture
def writer(tmp_path) -> TraceWriter:
    return TraceWriter(tmp_path / "traces.jsonl")


async def chat_with(services, writer, script, user="bob"):
    return await ChatSession.open(
        services,
        user_id=user,
        model=ScriptedLlm(script),
        session_service=InMemorySessionService(),
        resume=False,
        tracer=writer,
    )


def lines(writer: TraceWriter) -> list[dict]:
    return [json.loads(line) for line in writer.path.read_text().splitlines()]


async def test_a_turn_is_traced_end_to_end(services, writer) -> None:
    chat = await chat_with(
        services, writer, [call("run_query", sql="SELECT brand FROM products"), text("Levi's.")]
    )
    turn = await chat.ask("top brand?")
    spans = lines(writer)
    assert {s["trace_id"] for s in spans} == {turn.trace_id}
    assert [s["kind"] for s in spans] == [
        "user_message",
        "model_call",
        "tool_call",
        "model_call",
        "turn",
    ]
    tool = spans[2]
    assert tool["tool"] == "run_query"
    assert tool["status"] == "ok"
    assert tool["sql"] == "SELECT brand FROM products"
    assert tool["bytes_processed"] == 1_000_000
    assert tool["row_count"] == 1
    summary = spans[-1]
    assert summary["status"] == "ok"
    assert summary["user_id"] == "bob"
    assert summary["model_calls"] == 2
    assert summary["queries"] == 1
    assert summary["bytes_processed"] == 1_000_000
    assert summary["duration_ms"] >= 0


async def test_traces_hold_no_pii_and_no_result_rows(services, writer) -> None:
    chat = await chat_with(
        services,
        writer,
        [
            call("run_query", sql="SELECT * FROM users WHERE city = 'x' -- jane@corp.com"),
            text("Mail jane@corp.com"),
        ],
    )
    await chat.ask("my email is jane@corp.com, what about Levi's revenue?")
    raw = writer.path.read_text()
    assert "jane@corp.com" not in raw
    assert '"revenue": 10.0' not in raw  # result rows are data, never traced
    assert "Mail" not in raw  # answer text is not traced either
    summary = lines(writer)[-1]
    assert summary["redactions"] == ["email"]


async def test_failed_turn_is_traced(services, writer) -> None:
    chat = await chat_with(services, writer, [RuntimeError("boom")])
    turn = await chat.ask("hi")
    summary = lines(writer)[-1]
    assert summary["kind"] == "turn"
    assert summary["trace_id"] == turn.trace_id
    assert summary["status"] == "error"
    assert summary["error"] == "RuntimeError"


async def test_guard_rejections_and_query_errors_are_traced(services, writer) -> None:
    chat = await chat_with(
        services,
        writer,
        [
            call("run_query", sql="SELECT email FROM users"),
            call("run_query", sql="SELECT brand FROM products"),
            text("ok"),
        ],
    )
    services.executor.error = QueryFailed("invalid_query", "Unrecognized name")
    await chat.ask("q")
    tools = [s for s in lines(writer) if s["kind"] == "tool_call"]
    assert [(t["status"], t.get("rule") or t.get("error_kind")) for t in tools] == [
        ("rejected", "pii_column"),
        ("error", "invalid_query"),
    ]


async def test_delete_confirmation_is_traced(services, writer) -> None:
    services.store.save("bob", "c0", title="Client X Q1", body="secret body")
    chat = await chat_with(
        services, writer, [call("delete_reports", mentions="client x"), text("?"), text("done")]
    )
    turn = await chat.ask("delete client x")
    await chat.confirm(turn.confirmation, "delete 1")
    spans = lines(writer)
    tools = [s for s in spans if s["kind"] == "tool_call"]
    assert [t["status"] for t in tools] == ["awaiting_confirmation", "deleted"]
    turns = [s for s in spans if s["kind"] == "turn"]
    assert [t["status"] for t in turns] == ["awaiting_confirmation", "ok"]
    assert "secret body" not in writer.path.read_text()


def test_percentile() -> None:
    assert percentile([], 50) is None
    assert percentile([10], 95) == 10
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_summarize_computes_spec_metrics() -> None:
    def tool(trace, status, **kw):
        return {"kind": "tool_call", "trace_id": trace, "tool": "run_query", "status": status, **kw}

    spans = [
        # turn a: first query ok
        tool("a", "ok", row_count=3, bytes_processed=10_000_000, duration_ms=100),
        {
            "kind": "model_call",
            "trace_id": "a",
            "duration_ms": 900,
            "tokens_in": 1000,
            "tokens_out": 50,
            "attempts": [{"model": "p", "outcome": "ok"}],
        },
        {
            "kind": "turn",
            "trace_id": "a",
            "status": "ok",
            "duration_ms": 1000,
            "bytes_processed": 10_000_000,
            "redactions": [],
        },
        # turn b: rejected, then fixed (self-correction); fallback used; empty result
        tool("b", "rejected", rule="syntax", duration_ms=1),
        tool("b", "ok", row_count=0, bytes_processed=5_000_000, duration_ms=200),
        {
            "kind": "model_call",
            "trace_id": "b",
            "duration_ms": 3000,
            "tokens_in": 2000,
            "tokens_out": 80,
            "attempts": [
                {"model": "p", "outcome": "rate_limited"},
                {"model": "f", "outcome": "ok"},
            ],
        },
        {
            "kind": "turn",
            "trace_id": "b",
            "status": "ok",
            "duration_ms": 3000,
            "bytes_processed": 5_000_000,
            "redactions": ["email"],
        },
        # turn c: failed
        {
            "kind": "turn",
            "trace_id": "c",
            "status": "error",
            "duration_ms": 50,
            "bytes_processed": 0,
            "redactions": [],
            "error": "ModelUnavailable",
        },
        {"kind": "feedback", "trace_id": "a", "rating": "good"},
        {"kind": "feedback", "trace_id": "b", "rating": "bad"},
    ]
    m = summarize(spans)
    assert m["turns"] == 3
    assert m["turn_success_rate"] == pytest.approx(2 / 3)
    assert m["errors_by_type"] == {"ModelUnavailable": 1}
    assert m["sql_first_try_success_rate"] == pytest.approx(1 / 2)
    assert m["self_correction_rate"] == pytest.approx(1.0)  # of turns whose first try failed
    assert m["empty_result_rate"] == pytest.approx(1 / 2)
    assert m["guard_rejections_by_rule"] == {"syntax": 1}
    assert m["pii_redactions"] == 1
    assert m["model_fallbacks"] == 1
    assert m["model_attempts_by_outcome"] == {"ok": 2, "rate_limited": 1}
    assert m["tokens_in"] == 3000
    assert m["bytes_per_turn_avg"] == pytest.approx(5_000_000)
    assert m["turn_latency_ms"]["p50"] == 1000
    assert m["feedback"] == {"good": 1, "bad": 1}


async def test_cli_trace_stats_and_feedback(services, writer) -> None:
    chat = await chat_with(
        services, writer, [call("run_query", sql="SELECT brand FROM products"), text("Levi's.")]
    )
    console = Console(record=True, width=120)
    app = App(services, chat, console)
    await app.answer("top brand?")
    await app.handle("trace", "/trace")
    await app.handle("stats", "/stats")
    await app.handle("feedback", "/feedback bad wrong period, call me at 415-555-0134")
    out = console.export_text()
    assert "run_query" in out and "model_call" in out  # /trace timeline
    assert "Turns" in out  # /stats
    feedback = lines(writer)[-1]
    assert feedback["kind"] == "feedback"
    assert feedback["rating"] == "bad"
    assert feedback["trace_id"] == chat.last_turn.trace_id
    assert "415-555-0134" not in feedback["note"]
