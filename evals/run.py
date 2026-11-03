"""Run the evals against the real model and BigQuery (PLAN stage 9).

    uv run python -m evals.run [--suite scenarios|redteam|all] [--only id,id] [--pause 8]

Writes evals/results/<timestamp>/{results.json,summary.md,traces.jsonl} (git-ignored) and
prints the summary. Free tier: ~3-5 model calls per case, 15 requests/minute, so cases are
paced; a full run is about 30 cases.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import logging

from evals.harness import EVALS_DIR, CaseResult, Harness, load_cases
from retail_agent.agent.services import build_services
from retail_agent.config import Settings, load_env

SUITES = ("scenarios", "redteam")


def summary(results: list[CaseResult], started: str, model: str) -> str:
    lines = [f"# Eval run {started}", "", f"Model: `{model}` (fallback on 429/timeout).", ""]
    for suite in SUITES:
        rows = [r for r in results if r.suite == suite]
        if not rows:
            continue
        passed = sum(r.passed for r in rows)
        lines += [f"## {suite}: {passed}/{len(rows)} passed", ""]
        lines += ["| case | kind | user | result | model calls | queries | s | failures |"]
        lines += ["|---|---|---|---|---|---|---|---|"]
        for r in rows:
            failures = "; ".join(r.failures).replace("|", "\\|") or ""
            mark = "pass" if r.passed else "**FAIL**"
            lines.append(
                f"| {r.id} | {r.kind} | {r.user} | {mark} | {r.model_calls} | {r.queries} "
                f"| {r.duration_s} | {failures} |"
            )
        lines.append("")
    return "\n".join(lines)


async def main(args: argparse.Namespace) -> int:
    load_env()
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    services = build_services(settings)
    started = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = EVALS_DIR / "results" / started
    run_dir.mkdir(parents=True, exist_ok=True)
    harness = Harness(services, run_dir)
    only = set(args.only.split(",")) if args.only else None
    suites = SUITES if args.suite == "all" else (args.suite,)
    results: list[CaseResult] = []
    for suite in suites:
        for case in load_cases(suite):
            if only and case["id"] not in only:
                continue
            result = await harness.run(suite, case)
            results.append(result)
            mark = "pass" if result.passed else "FAIL " + "; ".join(result.failures)
            print(f"[{suite}] {case['id']}: {mark}", flush=True)
            # written after every case, so a stopped run keeps its answers
            (run_dir / "results.json").write_text(
                json.dumps([dataclasses.asdict(r) for r in results], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            await asyncio.sleep(args.pause)
    text = summary(results, started, settings.model)
    (run_dir / "summary.md").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)
    print(f"\nresults: {run_dir}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("all", *SUITES), default="all")
    parser.add_argument("--only", default="")
    parser.add_argument("--pause", type=float, default=8.0)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
