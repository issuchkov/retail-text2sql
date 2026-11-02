"""Business definitions: defaults, overrides, saved per user (SPEC §3.4, AC-11)."""

from __future__ import annotations

from google.adk.sessions import InMemorySessionService

from retail_agent.agent.definitions import DefinitionStore, normalize_term
from tests.unit.fakes import call, text
from tests.unit.test_cli import open_chat
from tests.unit.test_cli import services as services


def instruction_of(chat, index: int = -1) -> str:
    return chat._runner.agent.model.requests[index].config.system_instruction


def test_store_is_per_user() -> None:
    store = DefinitionStore(":memory:")
    store.set("bob", "churn", "no purchase in 60 days")
    assert store.all("bob") == {"churn": "no purchase in 60 days"}
    assert store.all("alice") == {}
    store.delete("bob", "churn")
    assert store.all("bob") == {}


def test_terms_are_normalized() -> None:
    assert normalize_term("  Churned Customer ") == "churn"
    assert normalize_term("Net revenue") == "revenue"
    assert normalize_term("Quarters") == "quarter"
    assert normalize_term("Active customer") == "active customer"


async def test_defaults_are_in_the_instruction_with_their_source(services) -> None:
    chat = await open_chat(services, [text("hi")])
    await chat.ask("hi")
    instruction = instruction_of(chat)
    assert "churn: no purchase in the 90 days" in instruction
    assert "(default)" in instruction


async def test_override_applies_to_this_conversation_only(services) -> None:
    sessions = InMemorySessionService()
    chat = await open_chat(
        services,
        [
            call("set_definition", term="churn", definition="no purchase in 60 days"),
            text("ok"),
            text("next"),
        ],
        sessions=sessions,
    )
    await chat.ask("use 60 days for churn")
    response = chat._runner.agent.model.function_responses()[-1]
    assert response == {"status": "set", "term": "churn", "scope": "this conversation"}
    await chat.ask("and now?")
    assert "churn: no purchase in 60 days (this conversation)" in instruction_of(chat)
    assert services.definitions.all("bob") == {}

    other = await open_chat(services, [text("x")], sessions=sessions, resume=False)
    await other.ask("new conversation")
    assert "no purchase in the 90 days" in instruction_of(other)


async def test_remembered_definition_applies_to_later_conversations(services) -> None:
    chat = await open_chat(
        services,
        [
            call(
                "set_definition", term="churn", definition="no purchase in 60 days", remember=True
            ),
            text("saved"),
        ],
    )
    await chat.ask("always use 60 days for churn")
    assert services.definitions.all("bob") == {"churn": "no purchase in 60 days"}

    later = await open_chat(services, [text("x")], resume=False)
    await later.ask("hi")
    assert "churn: no purchase in 60 days (your saved definition)" in instruction_of(later)

    alice = await open_chat(services, [text("x")], user="alice", resume=False)
    await alice.ask("hi")
    assert "no purchase in the 90 days" in instruction_of(alice)


async def test_reset_returns_to_the_default(services) -> None:
    services.definitions.set("bob", "churn", "no purchase in 60 days")
    chat = await open_chat(
        services,
        [
            call("set_definition", term="churn", definition="no purchase in 30 days"),
            call("reset_definition", term="churn"),
            text("reset"),
            text("x"),
        ],
    )
    await chat.ask("reset churn")
    await chat.ask("hi")
    assert services.definitions.all("bob") == {}
    assert "no purchase in the 90 days" in instruction_of(chat)


async def test_definitions_are_redacted(services) -> None:
    chat = await open_chat(
        services,
        [
            call(
                "set_definition",
                term="vip",
                definition="customers like jane@corp.com",
                remember=True,
            ),
            text("ok"),
        ],
    )
    await chat.ask("x")
    assert "jane@corp.com" not in services.definitions.all("bob")["vip"]
