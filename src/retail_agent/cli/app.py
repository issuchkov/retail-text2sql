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

from retail_agent.agent.services import Services, build_services
from retail_agent.cli.chat import ChatSession, Turn, footer
from retail_agent.config import Settings, load_env

COMMANDS = {"sql", "reports", "new", "quit", "help"}
HELP = """Ask questions about your brands' sales, products and customers in plain language.

Commands:
  /sql      show the SQL behind the last answer
  /reports  list your saved reports
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
        elif name == "reports":
            self.show_reports()
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
