"""Deep dive: print the full correspondence of a turn (SPEC §7, AC-15)."""

from __future__ import annotations

from retail_agent.observability.conversation import render_turns
from tests.unit.fakes import call, text
from tests.unit.test_cli import open_chat
from tests.unit.test_cli import services as services


async def test_render_turn_shows_the_whole_correspondence(services) -> None:
    chat = await open_chat(
        services,
        [
            call("run_query", sql="SELECT email FROM users"),
            call("run_query", sql="SELECT brand FROM products"),
            text("Levi's leads."),
            text("second turn"),
        ],
    )
    first = await chat.ask("which brand leads?")
    await chat.ask("anything else?")
    session = await chat._sessions.get_session(
        app_name="retail_agent", user_id="bob", session_id=chat.session_id
    )
    out = render_turns(session.events, trace_id=first.trace_id)
    assert "which brand leads?" in out
    assert "run_query" in out and "SELECT email FROM users" in out
    assert "rejected" in out and "pii_column" in out
    assert "Levi's leads." in out
    assert "second turn" not in out  # only the requested turn
    assert "anything else?" not in out

    everything = render_turns(session.events)
    assert "second turn" in everything
