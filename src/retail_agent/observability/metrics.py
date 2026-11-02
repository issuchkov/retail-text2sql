"""Metrics over trace spans (SPEC §7): what `/stats` shows and a dashboard would chart.

uv run python -m retail_agent.observability.metrics [logs/traces.jsonl]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

QUERY_FAILED = {"rejected", "error"}


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (the numpy default)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _latency(values: list[float]) -> dict[str, float | None]:
    return {"p50": percentile(values, 50), "p95": percentile(values, 95)}


def summarize(spans: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [
        s for s in spans if s.get("kind") == "turn" and s.get("status") != "awaiting_confirmation"
    ]
    models = [s for s in spans if s.get("kind") == "model_call"]
    tools = [s for s in spans if s.get("kind") == "tool_call"]
    queries = [s for s in tools if s.get("tool") == "run_query"]

    by_trace: dict[str, list[str]] = defaultdict(list)
    for q in queries:
        by_trace[q["trace_id"]].append(q.get("status", ""))
    first_ok = [statuses[0] == "ok" for statuses in by_trace.values()]
    failed_first = [s for s in by_trace.values() if s[0] in QUERY_FAILED]
    corrected = [s for s in failed_first if "ok" in s[1:]]
    ok_queries = [q for q in queries if q.get("status") == "ok"]

    attempts = [a for m in models for a in m.get("attempts") or []]
    fallbacks = 0
    for m in models:
        tried = m.get("attempts") or []
        succeeded = [a["model"] for a in tried if a.get("outcome") == "ok"]
        if tried and succeeded and succeeded[-1] != tried[0]["model"]:
            fallbacks += 1

    confirmations = Counter(
        t["status"]
        for t in tools
        if t.get("tool") == "delete_reports" and t.get("status") in {"deleted", "cancelled"}
    )
    n_turns = len(turns)
    return {
        "turns": n_turns,
        "turn_success_rate": _rate(sum(t.get("status") == "ok" for t in turns), n_turns),
        "errors_by_type": dict(
            Counter(t.get("error") for t in turns if t.get("status") == "error")
        ),
        "budget_exhausted_turns": sum(t.get("status") == "budget_exhausted" for t in turns),
        "sql_first_try_success_rate": _rate(sum(first_ok), len(first_ok)),
        "self_correction_rate": _rate(len(corrected), len(failed_first)),
        "empty_result_rate": _rate(
            sum((q.get("row_count") or 0) == 0 for q in ok_queries), len(ok_queries)
        ),
        "guard_rejections_by_rule": dict(
            Counter(t.get("rule") for t in tools if t.get("status") == "rejected")
        ),
        "pii_redactions": sum(bool(t.get("redactions")) for t in turns),
        "model_calls": len(models),
        "model_fallbacks": fallbacks,
        "model_attempts_by_outcome": dict(Counter(a.get("outcome") for a in attempts)),
        "tokens_in": sum(m.get("tokens_in") or 0 for m in models),
        "tokens_out": sum(m.get("tokens_out") or 0 for m in models),
        "bytes_processed": sum(t.get("bytes_processed") or 0 for t in turns),
        "bytes_per_turn_avg": _rate(sum(t.get("bytes_processed") or 0 for t in turns), n_turns),
        "turn_latency_ms": _latency(
            [t["duration_ms"] for t in turns if t.get("duration_ms") is not None]
        ),
        "model_latency_ms": _latency(
            [m["duration_ms"] for m in models if m.get("duration_ms") is not None]
        ),
        "tool_latency_ms": _latency(
            [t["duration_ms"] for t in tools if t.get("duration_ms") is not None]
        ),
        "confirmations": dict(confirmations),
        "feedback": dict(Counter(s.get("rating") for s in spans if s.get("kind") == "feedback")),
    }


def _rate(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def main(argv: list[str]) -> None:
    path = Path(argv[0]) if argv else Path("logs/traces.jsonl")
    spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    print(json.dumps(summarize(spans), indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
