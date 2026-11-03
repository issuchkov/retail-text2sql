"""Answer-format preferences per user (SPEC §3.5, AC-13)."""

from __future__ import annotations

import pytest

from retail_agent.agent.preferences import PreferenceStore, normalize_preference
from tests.unit.fakes import call, text
from tests.unit.test_cli import open_chat
from tests.unit.test_cli import services as services


def instruction_of(chat, index: int = -1) -> str:
    return chat._runner.agent.model.requests[index].config.system_instruction


def test_store_is_per_user() -> None:
    store = PreferenceStore(":memory:")
    store.set("bob", "format", "bullets")
    store.set("bob", "depth", "brief")
    assert store.all("bob") == {"depth": "brief", "format": "bullets"}
    assert store.all("alice") == {}
    store.delete("bob", "format")
    assert store.all("bob") == {"depth": "brief"}


@pytest.mark.parametrize(
    ("aspect", "value", "expected"),
    [
        ("format", "bullet points", ("format", "bullets")),
        ("Format", "Tables", ("format", "tables")),
        ("depth", "short", ("depth", "brief")),
        ("depth", "deep dive", ("depth", "detailed")),
        ("visuals", "charts", ("visuals", "charts")),
        ("charts", "yes", None),
        ("format", "emoji", None),
    ],
)
def test_normalize_preference(aspect, value, expected) -> None:
    assert normalize_preference(aspect, value) == expected


async def test_saved_preference_shapes_later_conversations(services) -> None:
    chat = await open_chat(
        services, [call("remember_preference", aspect="format", value="bullet points"), text("ok")]
    )
    await chat.ask("I prefer bullet points over tables")
    response = chat._runner.agent.model.function_responses()[-1]
    assert response == {"status": "saved", "aspect": "format", "value": "bullets"}

    later = await open_chat(services, [text("x")], resume=False)
    await later.ask("revenue?")
    instruction = instruction_of(later)
    assert "format: bullets" in instruction
    assert "bullet points" in instruction.lower()

    alice = await open_chat(services, [text("x")], user="alice", resume=False)
    await alice.ask("revenue?")
    assert "format: bullets" not in instruction_of(alice)


async def test_invalid_preference_is_not_saved(services) -> None:
    chat = await open_chat(
        services, [call("remember_preference", aspect="format", value="emoji"), text("ok")]
    )
    await chat.ask("use emoji")
    response = chat._runner.agent.model.function_responses()[-1]
    assert response["status"] == "invalid"
    assert "bullets" in response["allowed"]["format"]
    assert services.preferences.all("bob") == {}


async def test_default_removes_a_preference(services) -> None:
    services.preferences.set("bob", "depth", "brief")
    chat = await open_chat(
        services,
        [call("remember_preference", aspect="depth", value="default"), text("ok"), text("x")],
    )
    await chat.ask("go back to normal depth")
    assert services.preferences.all("bob") == {}
    await chat.ask("next")
    assert "depth: brief" not in instruction_of(chat)
