"""Agent wiring with a scripted model and the real ADK runner (SPEC §2, §3, §4.5, §5.3)."""

from __future__ import annotations

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from retail_agent.agent.agent import APP_NAME, build_agent
from retail_agent.agent.services import Services
from retail_agent.config import Entitlement, Settings, UserProfile
from retail_agent.query.executor import QueryFailed
from retail_agent.reports.store import ReportStore
from tests.unit.fakes import FakeExecutor, ScriptedLlm, call, text

CONFIRMATION = "adk_request_confirmation"


@pytest.fixture
def persona(tmp_path):
    path = tmp_path / "persona.md"
    path.write_text("Speak like a concise analyst.", encoding="utf-8")
    return path


@pytest.fixture
def services(persona, tmp_path) -> Services:
    users = {
        "bob": UserProfile(
            id="bob", name="Bob", title="Denim", scope=Entitlement(brands=["Levi's", "Wrangler"])
        ),
        "alice": UserProfile(
            id="alice", name="Alice", title="CK", scope=Entitlement(brands=["Calvin Klein"])
        ),
    }
    settings = Settings(
        RETAIL_PERSONA_FILE=persona, RETAIL_PSEUDONYM_SALT="salt", RETAIL_DATA_DIR=tmp_path
    )
    return Services(
        settings=settings, executor=FakeExecutor(), store=ReportStore(":memory:"), users=users
    )


class Chat:
    def __init__(self, services: Services, script, user: str = "bob") -> None:
        self.llm = ScriptedLlm(script)
        self.sessions = InMemorySessionService()
        self.runner = Runner(
            app_name=APP_NAME,
            agent=build_agent(services, model=self.llm),
            session_service=self.sessions,
        )
        self.user = user
        self.session_id: str | None = None
        self.pending = None  # (event, function_call) awaiting confirmation

    async def send(self, message: types.Content, invocation_id: str | None = None) -> list:
        if self.session_id is None:
            session = await self.sessions.create_session(app_name=APP_NAME, user_id=self.user)
            self.session_id = session.id
        events = []
        async for event in self.runner.run_async(
            user_id=self.user,
            session_id=self.session_id,
            new_message=message,
            invocation_id=invocation_id,
        ):
            events.append(event)
            for fc in event.get_function_calls():
                if fc.name == CONFIRMATION:
                    self.pending = (event, fc)
        return events

    async def say(self, value: str) -> list:
        return await self.send(types.Content(role="user", parts=[types.Part(text=value)]))

    async def confirm(self, typed: str) -> list:
        event, fc = self.pending
        self.pending = None
        response = types.FunctionResponse(
            id=fc.id, name=fc.name, response={"confirmed": bool(typed), "payload": {"typed": typed}}
        )
        return await self.send(
            types.Content(role="user", parts=[types.Part(function_response=response)]),
            invocation_id=event.invocation_id,
        )


def final_text(events) -> str:
    return "".join(p.text for e in events if e.content for p in e.content.parts or [] if p.text)


async def test_queries_run_with_the_session_users_scope(services: Services) -> None:
    chat = Chat(services, [call("run_query", sql="SELECT brand FROM products"), text("done")])
    await chat.say("which brands do I have?")
    (query,) = services.executor.queries
    params = {p.name: p.value for p in query.params}
    assert params["scope_brands"] == ["Levi's", "Wrangler"]
    (response,) = chat.llm.function_responses()
    assert response["status"] == "ok"
    assert response["rows"] == [{"brand": "Levi's", "revenue": 10.0}]


async def test_tools_do_not_accept_identity_from_the_model(services: Services) -> None:
    agent = build_agent(services, model=ScriptedLlm([]))
    for tool in agent.tools:
        declaration = tool._get_declaration()
        properties = declaration.parameters.properties if declaration.parameters else {}
        assert not {"user_id", "user", "scope", "brands", "owner_id"} & set(properties or {})


async def test_guard_rejection_goes_back_to_the_model(services: Services) -> None:
    chat = Chat(services, [call("run_query", sql="SELECT email FROM users"), text("sorry")])
    await chat.say("emails please")
    assert services.executor.queries == []
    (response,) = chat.llm.function_responses()
    assert response["status"] == "rejected"
    assert response["rule"] == "pii_column"


async def test_query_failure_is_classified_for_the_model(services: Services) -> None:
    services.executor.error = QueryFailed("invalid_query", "Unrecognized name: revenue")
    chat = Chat(services, [call("run_query", sql="SELECT revenue FROM orders"), text("hm")])
    await chat.say("revenue?")
    (response,) = chat.llm.function_responses()
    assert response == {
        "status": "error",
        "kind": "invalid_query",
        "message": "Unrecognized name: revenue",
        "can_fix_by_rewriting_sql": True,
    }


async def test_empty_result_carries_a_hint(services: Services) -> None:
    services.executor.rows = []
    chat = Chat(services, [call("run_query", sql="SELECT brand FROM products"), text("none")])
    await chat.say("x")
    (response,) = chat.llm.function_responses()
    assert response["status"] == "ok"
    assert response["row_count"] == 0
    assert "list_column_values" in response["hint"]


async def test_list_column_values_goes_through_the_guard(services: Services) -> None:
    chat = Chat(
        services,
        [
            call("list_column_values", table="products", column="category"),
            call("list_column_values", table="users", column="email"),
            text("ok"),
        ],
    )
    await chat.say("which categories?")
    first, second = chat.llm.function_responses()
    assert first["status"] == "ok"
    assert len(services.executor.queries) == 1
    assert "products" in services.executor.queries[0].sql
    assert second["status"] == "rejected"


async def test_answer_is_redacted(services: Services) -> None:
    chat = Chat(services, [text("Write to top.buyer@example.com about the promo")])
    events = await chat.say("who should I email?")
    answer = final_text(events)
    assert "example.com" not in answer
    assert "[redacted]" in answer


async def test_instruction_has_persona_scope_and_is_reread_each_turn(
    services: Services, persona
) -> None:
    chat = Chat(services, [text("one"), text("two")])
    await chat.say("hi")
    persona.write_text("Answer in pirate speak.", encoding="utf-8")
    await chat.say("hi again")
    first, second = (r.config.system_instruction for r in chat.llm.requests)
    assert "concise analyst" in first
    assert "pirate speak" in second
    assert "Levi's, Wrangler" in first
    assert "order_revenue" in first  # schema
    assert "90 days" in first  # default churn definition


async def test_save_and_list_reports_are_owner_scoped(services: Services) -> None:
    services.store.save("alice", "c0", title="Alice's Client X", body="x")
    chat = Chat(
        services,
        [
            call(
                "save_report",
                title="Q3 denim review",
                body="Revenue up. Contact buyer@example.com",
                entities=["Levi's"],
            ),
            call("list_reports"),
            text("saved"),
        ],
    )
    await chat.say("save it")
    saved, listed = chat.llm.function_responses()
    assert saved["status"] == "saved"
    stored = services.store.get("bob", saved["report_id"])
    assert "example.com" not in stored.body  # no PII in saved reports
    assert [r["title"] for r in listed["reports"]] == ["Q3 denim review"]


async def test_delete_requires_the_typed_phrase_and_deletes_the_previewed_reports(
    services: Services,
) -> None:
    a = services.store.save("bob", "c0", title="Client X Q1", body="")
    b = services.store.save("bob", "c0", title="Client X Q2", body="")
    other = services.store.save("alice", "c0", title="Client X for Alice", body="")
    chat = Chat(
        services, [call("delete_reports", mentions="Client X"), text("asked"), text("done")]
    )

    await chat.say("delete all reports about Client X")
    assert chat.pending is not None
    _, fc = chat.pending
    preview = fc.args["toolConfirmation"]["payload"]
    assert [i["id"] for i in preview["items"]] == [a.id, b.id]
    assert preview["phrase"] == "delete 2"
    waiting = chat.llm.function_responses()[-1]
    assert waiting["status"] == "awaiting_confirmation"
    assert "Nothing has been deleted" in waiting["message"]
    assert len(services.store.list("bob")) == 2  # nothing deleted before confirming

    await chat.confirm("delete 2")
    result = chat.llm.function_responses()[-1]
    assert result == {"status": "deleted", "deleted_ids": [a.id, b.id]}
    assert services.store.list("bob") == []
    assert services.store.get("alice", other.id) is not None


async def test_delete_with_wrong_phrase_is_cancelled(services: Services) -> None:
    services.store.save("bob", "c0", title="Client X Q1", body="")
    chat = Chat(services, [call("delete_reports", mentions="client x"), text("asked"), text("ok")])
    await chat.say("delete client x")
    await chat.confirm("yes")
    assert chat.llm.function_responses()[-1]["status"] == "cancelled"
    assert len(services.store.list("bob")) == 1


async def test_delete_with_no_match_does_not_ask(services: Services) -> None:
    chat = Chat(services, [call("delete_reports", mentions="nothing"), text("none")])
    await chat.say("delete nothing")
    assert chat.pending is None
    assert chat.llm.function_responses()[-1]["status"] == "nothing_matched"


async def test_delete_needs_a_selector(services: Services) -> None:
    chat = Chat(services, [call("delete_reports"), text("which?")])
    await chat.say("delete")
    assert chat.pending is None
    assert chat.llm.function_responses()[-1]["status"] == "invalid_selector"


async def test_invented_figures_are_flagged(services: Services) -> None:
    chat = Chat(services, [text("Your 2025 net revenue was $144,369,637.38.")])
    events = await chat.say("What was my 2025 revenue?")
    answer = final_text(events)
    assert "$144,369,637.38" in answer  # the model's text is kept ...
    assert "Unverified figures" in answer  # ... with a visible warning


async def test_figures_from_query_results_are_not_flagged(services: Services) -> None:
    services.executor.rows = [{"brand": "Levi's", "net_revenue": 21603.85}]
    chat = Chat(
        services,
        [call("run_query", sql="SELECT brand FROM products"), text("Levi's made $21,603.85.")],
    )
    events = await chat.say("Levi's revenue?")
    assert "Unverified" not in final_text(events)


async def test_get_report_returns_only_own_reports(services: Services) -> None:
    mine = services.store.save("bob", "c0", title="Q3 denim review", body="Revenue grew.")
    theirs = services.store.save("alice", "c0", title="CK pricing", body="secret plan")
    chat = Chat(
        services,
        [
            call("get_report", report_id=mine.id),
            call("get_report", report_id=theirs.id),
            text("ok"),
        ],
    )
    await chat.say("open my report")
    own, other = chat.llm.function_responses()
    assert own["status"] == "ok"
    assert own["title"] == "Q3 denim review"
    assert own["body"] == "Revenue grew."
    assert other["status"] == "not_found"
    assert "secret" not in str(other)


async def test_second_delete_while_one_is_pending_is_refused(services: Services) -> None:
    services.store.save("bob", "c0", title="Alpha review", body="")
    services.store.save("bob", "c0", title="Beta review", body="")
    chat = Chat(
        services,
        [
            call("delete_reports", mentions="Alpha"),
            call("delete_reports", mentions="Beta"),
            text("Please confirm Alpha first."),
        ],
    )
    await chat.say("delete the alpha and beta reports")
    first, second = chat.llm.function_responses()
    assert first["status"] == "awaiting_confirmation"
    assert second["status"] == "one_at_a_time"
    assert "confirm" in second["message"].lower()
    assert len(services.store.list("bob")) == 2


async def test_unanswered_pending_delete_from_an_earlier_turn_does_not_block(
    services: Services,
) -> None:
    services.store.save("bob", "c0", title="Alpha review", body="")
    chat = Chat(
        services,
        [
            call("delete_reports", mentions="Alpha"),
            text("asked"),
            call("delete_reports", mentions="Alpha"),
            text("asked again"),
        ],
    )
    await chat.say("delete alpha")
    chat.pending = None  # the user walked away without answering
    await chat.say("delete alpha")
    assert chat.llm.function_responses()[-1]["status"] == "awaiting_confirmation"


async def test_row_cap_is_reported_as_reached_not_truncated(services: Services) -> None:
    services.settings.max_result_rows = 2
    services.executor.rows = [{"n": 1}, {"n": 2}]
    chat = Chat(services, [call("run_query", sql="SELECT brand FROM products"), text("ok")])
    await chat.say("x")
    (response,) = chat.llm.function_responses()
    assert response["row_limit_reached"] is True
    assert "truncated" not in response


async def test_instruction_starts_with_the_static_part(services: Services) -> None:
    """Static rules and schema first, so a cached prefix is shared across users and days."""
    chat = Chat(services, [text("one")])
    await chat.say("hi")
    instruction = chat.llm.requests[0].config.system_instruction
    rules_at = instruction.index("How you work:")
    schema_at = instruction.index("Tables (BigQuery Standard SQL)")
    persona_at = instruction.index("concise analyst")
    user_at = instruction.index("You are talking to")
    assert rules_at < schema_at < persona_at < user_at
