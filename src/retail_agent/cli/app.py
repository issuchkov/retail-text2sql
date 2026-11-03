"""Terminal chat: `retail-agent chat --user <id>` (SPEC §3.3, §5.3)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table

from retail_agent.agent import definitions
from retail_agent.agent.services import Services, build_services
from retail_agent.cli.chat import ChatSession, Turn, footer
from retail_agent.config import Settings, load_env
from retail_agent.observability.metrics import summarize

COMMANDS = {
    "sql",
    "trace",
    "stats",
    "feedback",
    "definitions",
    "preferences",
    "persona",
    "reports",
    "report",
    "new",
    "quit",
    "help",
}
HELP = """Ask questions about your brands' sales, products and customers in plain language.

Commands:
  /sql      show the SQL behind the last answer
  /trace    the steps of the last answer: model calls, queries, timings
  /stats    metrics for this conversation
  /feedback good|bad [note]   rate the last answer
  /definitions  business definitions in effect and where they come from
  /preferences  how you like answers (tables, bullets, depth, charts)
  /persona  the agent's tone; persona editors: /persona set <text>, /persona history
  /reports  list your saved reports
  /report <id>  show one of your saved reports
  /new      start a new conversation
  /help     this help
  /quit     leave (also Ctrl-D)"""


def parse_command(line: str) -> tuple[str, str] | None:
    """Return (command, argument) for a slash command, ("empty", "") or None for a question."""
    stripped = line.strip()
    if not stripped:
        return ("empty", "")
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    name = head.lower()
    if name == "exit":
        name = "quit"
    if name not in COMMANDS:
        return ("unknown", stripped)
    return (name, rest.strip())


class App:
    def __init__(self, services: Services, chat: ChatSession, console: Console) -> None:
        self.services = services
        self.chat = chat
        self.console = console

    def banner(self) -> None:
        user = self.services.user(self.chat.user_id)
        self.console.print(
            f"[bold]Retail analyst[/] — {user.name}, {user.title}. Scope: {user.scope.describe()}."
        )
        if self.chat.resumed:
            self.console.print("[dim]Continuing your last conversation. /new starts a new one.[/]")
        self.console.print("[dim]/help for commands.[/]\n")

    async def run(self) -> None:
        self.banner()
        while True:
            try:
                line = await asyncio.to_thread(self.console.input, "[bold cyan]you>[/] ")
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return
            command = parse_command(line)
            if command is None:
                await self.answer(line)
                continue
            name, _ = command
            if name == "quit":
                return
            await self.handle(name, line)

    async def handle(self, name: str, line: str) -> None:
        if name == "empty":
            return
        if name == "help":
            self.console.print(HELP)
        elif name == "sql":
            self.show_sql()
        elif name == "trace":
            self.show_trace()
        elif name == "stats":
            self.show_stats()
        elif name == "feedback":
            self.record_feedback(line)
        elif name == "preferences":
            self.show_preferences()
        elif name == "persona":
            self.persona(line)
        elif name == "definitions":
            await self.show_definitions()
        elif name == "reports":
            self.show_reports()
        elif name == "report":
            self.show_report(line)
        elif name == "new":
            await self.chat.new_conversation()
            self.console.print("[dim]New conversation.[/]")
        else:
            self.console.print(f"Unknown command {line.strip()}. /help lists the commands.")

    async def answer(self, question: str) -> None:
        try:
            with self.console.status("Thinking…"):
                turn = await self.chat.ask(question)
        except KeyboardInterrupt:
            self.console.print("[dim]Cancelled.[/]")
            return
        while True:
            self.render(turn)
            if turn.confirmation is None:
                return
            typed = await self.ask_confirmation(turn)
            with self.console.status("Working…"):
                turn = await self.chat.confirm(turn.confirmation, typed)

    async def ask_confirmation(self, turn: Turn) -> str:
        confirmation = turn.confirmation
        assert confirmation is not None
        table = Table(title="Reports to delete permanently", show_lines=False)
        for column in ("id", "title", "created"):
            table.add_column(column)
        for item in confirmation.items:
            table.add_row(str(item.get("id")), str(item.get("title")), str(item.get("created")))
        self.console.print(table)
        prompt = f"Type [bold]{confirmation.phrase}[/] to delete, anything else cancels: "
        try:
            return await asyncio.to_thread(self.console.input, prompt)
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return ""

    def render(self, turn: Turn) -> None:
        if turn.error:
            self.console.print(f"[red]{turn.error}[/]")
            return
        if turn.answer:
            self.console.print(Markdown(turn.answer))
        self.console.print(f"[dim]{footer(turn)}[/]\n")

    def show_sql(self) -> None:
        turn = self.chat.last_turn
        if not turn or not turn.queries:
            self.console.print("No queries in the last answer.")
            return
        for i, sql in enumerate(turn.queries, 1):
            self.console.print(f"[dim]-- query {i} (your brand scope is applied server-side)[/]")
            self.console.print(Syntax(sql, "sql", word_wrap=True))

    def show_trace(self) -> None:
        turn = self.chat.last_turn
        spans = self.chat.tracer.read(trace_id=turn.trace_id) if turn else []
        if not spans:
            self.console.print("No trace for the last answer yet.")
            return
        table = Table(title=f"trace {turn.trace_id}")
        for column in ("at ms", "step", "detail", "ms"):
            table.add_column(column)
        for span in spans:
            table.add_row(
                str(span.get("at_ms", "")),
                str(span.get("kind")),
                _span_detail(span),
                "" if span.get("duration_ms") is None else str(span["duration_ms"]),
            )
        self.console.print(table)

    def show_stats(self) -> None:
        m = summarize(self.chat.tracer.read(session_id=self.chat.session_id))
        table = Table(title="This conversation")
        table.add_column("metric")
        table.add_column("value")
        rows = [
            ("Turns", m["turns"]),
            ("Turns by status", _counts(m["turns_by_status"])),
            ("Data answers that succeeded", _pct(m["data_answer_success_rate"])),
            ("Answered without data (refusals, meta)", _pct(m["no_query_share"])),
            ("SQL first-try success", _pct(m["sql_first_try_success_rate"])),
            ("Self-corrected after a failed query", _pct(m["self_correction_rate"])),
            ("Empty results", _pct(m["empty_result_rate"])),
            ("Guard rejections", _counts(m["guard_rejections_by_rule"])),
            ("PII redactions (turns)", m["pii_redactions"]),
            ("Answers with unverified figures", m["ungrounded_figure_turns"]),
            ("Model calls / fallbacks", f"{m['model_calls']} / {m['model_fallbacks']}"),
            ("Model attempts", _counts(m["model_attempts_by_outcome"])),
            ("Tokens in / out", f"{m['tokens_in']} / {m['tokens_out']}"),
            ("BigQuery MB per turn", _mb(m["bytes_per_turn_avg"])),
            ("BigQuery MB billed (total)", _mb(m["bytes_billed"])),
            ("Turn latency p50 / p95 ms", _p(m["turn_latency_ms"])),
            ("Model call latency p50 / p95 ms", _p(m["model_latency_ms"])),
            ("Tool latency p50 / p95 ms", _p(m["tool_latency_ms"])),
            ("Deletes confirmed / cancelled", _counts(m["confirmations"])),
            ("Errors", _counts(m["errors_by_type"])),
            ("Feedback", _counts(m["feedback"])),
        ]
        for label, value in rows:
            table.add_row(label, str(value))
        self.console.print(table)

    def record_feedback(self, line: str) -> None:
        _, _, rest = line.strip().partition(" ")
        rating, _, note = rest.strip().partition(" ")
        rating = rating.lower()
        if rating not in {"good", "bad"}:
            self.console.print("Usage: /feedback good|bad [note]")
            return
        turn = self.chat.last_turn
        if turn is None:
            self.console.print("Ask something first, then rate the answer.")
            return
        self.chat.tracer.write(
            {
                "kind": "feedback",
                "trace_id": turn.trace_id,
                "session_id": self.chat.session_id,
                "user_id": self.chat.user_id,
                "rating": rating,
                "note": note.strip()[:1000],
            }
        )
        self.console.print("[dim]Thanks — feedback recorded.[/]")

    async def show_definitions(self) -> None:
        state = await self.chat.session_state()
        rows = definitions.effective(state, self.services.definitions, self.chat.user_id)
        table = Table(title="Business definitions in effect")
        for column in ("term", "definition", "source"):
            table.add_column(column)
        for term, text, source in rows:
            table.add_row(term, text, source)
        self.console.print(table)

    def show_preferences(self) -> None:
        saved = self.services.preferences.all(self.chat.user_id)
        if not saved:
            self.console.print(
                'No saved preferences. Tell me, e.g. "I prefer bullet points" or "keep it brief".'
            )
            return
        table = Table(title="Your answer preferences")
        table.add_column("aspect")
        table.add_column("value")
        for aspect, value in saved.items():
            table.add_row(aspect, value)
        self.console.print(table)

    def persona(self, line: str) -> None:
        store = self.services.persona
        assert store is not None
        _, _, rest = line.strip().partition(" ")
        action, _, text = rest.strip().partition(" ")
        if not action:
            self.console.print(Markdown(store.read() or "(no persona set)"))
            return
        user = self.services.user(self.chat.user_id)
        if not user.persona_editor:
            self.console.print("Only a persona editor can change the persona.")
            return
        if action == "set" and text.strip():
            store.set(text, author=user.id)
            self.console.print("[dim]Persona updated; it applies from the next answer.[/]")
        elif action == "history":
            versions = store.history()
            if not versions:
                self.console.print("No earlier versions.")
                return
            table = Table(title="Replaced persona versions (newest first)")
            for column in ("replaced at", "replaced by", "text"):
                table.add_column(column)
            for v in versions:
                table.add_row(v.replaced_at, v.author, " ".join(v.text.split())[:120])
            self.console.print(table)
        else:
            self.console.print("Usage: /persona | /persona set <text> | /persona history")

    def show_report(self, line: str) -> None:
        _, _, arg = line.strip().partition(" ")
        if not arg.strip().isdigit():
            self.console.print("Usage: /report <id>  (ids are listed by /reports)")
            return
        report = self.services.store.get(self.chat.user_id, int(arg))
        if report is None:
            self.console.print(f"You have no saved report {arg.strip()}.")
            return
        created = report.created_at.date().isoformat()
        self.console.print(f"[bold]{report.title}[/]  [dim]#{report.id} · {created}[/]")
        self.console.print(Markdown(report.body))

    def show_reports(self) -> None:
        reports = self.services.store.list(self.chat.user_id)
        if not reports:
            self.console.print("You have no saved reports.")
            return
        table = Table(title="Your saved reports")
        for column in ("id", "title", "created", "entities"):
            table.add_column(column)
        for r in reports:
            table.add_row(
                str(r.id), r.title, r.created_at.date().isoformat(), ", ".join(r.entities)
            )
        self.console.print(table)


def _span_detail(span: dict) -> str:
    kind = span.get("kind")
    if kind == "model_call":
        attempts = ", ".join(f"{a['model']}:{a['outcome']}" for a in span.get("attempts") or [])
        calls = ",".join(span.get("function_calls") or []) or "answer"
        return (
            f"{span.get('model')} → {calls} · {span.get('tokens_in')}/{span.get('tokens_out')} tok"
            + (f" · {attempts}" if attempts and len(span.get("attempts") or []) > 1 else "")
        )
    if kind == "tool_call":
        bits = [str(span.get("tool")), str(span.get("status"))]
        for key in ("rule", "error_kind", "row_count", "method"):
            if span.get(key) is not None:
                bits.append(f"{key}={span[key]}")
        if span.get("bytes_processed"):
            bits.append(f"{span['bytes_processed'] / 1e6:.0f} MB")
        return " ".join(bits)
    if kind == "turn":
        calls, queries = span.get("model_calls"), span.get("queries")
        return f"{span.get('status')} · {calls} model calls · {queries} queries"
    if kind == "user_message":
        return (
            "confirmation reply" if span.get("confirmation_reply") else f"{span.get('chars')} chars"
        )
    return str(span.get("error") or "")


NA = "-"


def _pct(value: float | None) -> str:
    return NA if value is None else f"{value:.0%}"


def _mb(value: float | None) -> str:
    return NA if value is None else f"{value / 1e6:.0f}"


def _p(latency: dict) -> str:
    def fmt(v):
        return NA if v is None else f"{v:.0f}"

    return f"{fmt(latency['p50'])} / {fmt(latency['p95'])}"


def _counts(counts: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or NA


def _configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(settings.log_dir / "cli.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("retail_agent").setLevel(logging.INFO)
    logging.captureWarnings(True)


async def _chat(args: argparse.Namespace, console: Console) -> int:
    load_env()
    settings = Settings()
    _configure_logging(settings)
    if not os.environ.get("GOOGLE_API_KEY"):
        console.print("[red]GOOGLE_API_KEY is not set. Copy .env.example to .env and add it.[/]")
        return 2
    try:
        services = build_services(settings)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 2
    if args.user not in services.users:
        known = ", ".join(sorted(services.users))
        console.print(f"[red]Unknown user {args.user!r}. Configured users: {known}.[/]")
        return 2
    chat = await ChatSession.open(services, user_id=args.user, resume=not args.new)
    await App(services, chat, console).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retail-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="chat with the retail data analyst")
    chat.add_argument("--user", required=True, help="identity from config/users.yaml")
    chat.add_argument("--new", action="store_true", help="start a new conversation")
    args = parser.parse_args(argv)
    return asyncio.run(_chat(args, Console()))


if __name__ == "__main__":
    sys.exit(main())
