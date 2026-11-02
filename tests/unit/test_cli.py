"""CLI chat session: turns, confirmation, errors, resume (SPEC §3.3, §5.3, §6)."""

from __future__ import annotations

import pytest
from google.adk.sessions import DatabaseSessionService, InMemorySessionService

from retail_agent.agent.services import Services
from retail_agent.cli.app import parse_command
from retail_agent.cli.chat import ChatSession, footer
from retail_agent.config import Entitlement, Settings, UserProfile
from retail_agent.reports.store import ReportStore
from tests.unit.fakes import FakeExecutor, ScriptedLlm, call, text


@pytest.fixture
def services(tmp_path) -> Services:
    persona = tmp_path / "persona.md"
    persona.write_text("Be brief.", encoding="utf-8")
    users = {
        u: UserProfile(id=u, name=u.title(), scope=Entitlement(brands=["Levi's"]))
        for u in ("bob", "alice")
    }
    return Services(
        settings=Settings(RETAIL_PERSONA_FILE=persona, RETAIL_PSEUDONYM_SALT="salt"),
        executor=FakeExecutor(),
        store=ReportStore(":memory:"),
        users=users,
    )


async def open_chat(services, script, *, user="bob", sessions=None, resume=True):
    return await ChatSession.open(
        services,
        user_id=user,
        model=ScriptedLlm(script),
        session_service=sessions or InMemorySessionService(),
        resume=resume,
    )


async def test_turn_has_answer_queries_and_trace(services) -> None:
    chat = await open_chat(
        services, [call("run_query", sql="SELECT brand FROM products"), text("Levi's leads.")]
    )
    turn = await chat.ask("top brand?")
    assert turn.answer == "Levi's leads."
    assert turn.queries == ["SELECT brand FROM products"]
    assert turn.bytes_processed == 1_000_000
    assert turn.trace_id
    assert turn.error is None
    assert footer(turn) == f"trace {turn.trace_id} · 1 query · 1 MB"


async def test_confirmation_is_surfaced_and_resolved(services) -> None:
    report = services.store.save("bob", "c0", title="Client X Q1", body="")
    chat = await open_chat(
        services,
        [call("delete_reports", mentions="client x"), text("Please confirm."), text("Done.")],
    )
    turn = await chat.ask("delete client x reports")
    assert turn.confirmation is not None
    assert turn.confirmation.phrase == "delete 1"
    assert [i["id"] for i in turn.confirmation.items] == [report.id]
    assert services.store.list("bob")  # still there

    done = await chat.confirm(turn.confirmation, "delete 1")
    assert done.answer == "Done."
    assert services.store.list("bob") == []


async def test_wrong_phrase_cancels(services) -> None:
    services.store.save("bob", "c0", title="Client X Q1", body="")
    chat = await open_chat(
        services, [call("delete_reports", mentions="client x"), text("Confirm?"), text("Kept.")]
    )
    turn = await chat.ask("delete client x")
    await chat.confirm(turn.confirmation, "delete all")
    assert len(services.store.list("bob")) == 1


async def test_errors_become_a_message_and_the_chat_continues(services) -> None:
    chat = await open_chat(services, [RuntimeError("boom"), text("Back again.")])
    failed = await chat.ask("hello")
    assert failed.error is not None
    assert failed.trace_id in failed.error
    assert "boom" not in failed.error  # internals are logged, not shown
    ok = await chat.ask("hello again")
    assert ok.answer == "Back again."


async def test_resume_returns_to_the_users_last_conversation(services, tmp_path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'sessions.sqlite'}"
    first = await open_chat(services, [text("one")], sessions=DatabaseSessionService(db_url=url))
    await first.ask("hi")

    again = await open_chat(services, [], sessions=DatabaseSessionService(db_url=url))
    assert again.session_id == first.session_id
    assert again.resumed

    fresh = await open_chat(services, [], sessions=DatabaseSessionService(db_url=url), resume=False)
    assert fresh.session_id != first.session_id

    alice = await open_chat(services, [], user="alice", sessions=DatabaseSessionService(db_url=url))
    assert alice.session_id not in {first.session_id, fresh.session_id}
    assert not alice.resumed


async def test_new_conversation_switches_session(services) -> None:
    chat = await open_chat(services, [text("a")])
    before = chat.session_id
    await chat.new_conversation()
    assert chat.session_id != before


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/sql", ("sql", "")),
        ("  /REPORTS ", ("reports", "")),
        ("/new", ("new", "")),
        ("/quit", ("quit", "")),
        ("/exit", ("quit", "")),
        ("/help", ("help", "")),
        ("/nope", ("unknown", "/nope")),
        ("how are sales?", None),
        ("", ("empty", "")),
    ],
)
def test_parse_command(line: str, expected) -> None:
    assert parse_command(line) == expected
