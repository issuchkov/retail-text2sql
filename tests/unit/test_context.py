"""Long conversations stay bounded; too-long requests get a clear message (SPEC §6)."""

from __future__ import annotations

from google.genai import errors as genai_errors
from google.genai import types

from retail_agent.agent.context import compact_history
from tests.unit.fakes import call, text
from tests.unit.test_cli import open_chat
from tests.unit.test_cli import services as services


def user(value: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=value)])


def result(rows: list) -> types.Content:
    response = {"status": "ok", "columns": ["n"], "rows": rows, "row_count": len(rows)}
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(name="run_query", response=response)
            )
        ],
    )


def test_tool_results_of_earlier_turns_are_compacted() -> None:
    contents = [user("q1"), result([{"n": 1}, {"n": 2}]), user("q2"), result([{"n": 3}])]
    compacted = compact_history(contents)
    old = compacted[1].parts[0].function_response.response
    current = compacted[3].parts[0].function_response.response
    assert "rows" not in old
    assert old["row_count"] == 2 and old["status"] == "ok" and old["columns"] == ["n"]
    assert "compacted" in old["note"]
    assert current["rows"] == [{"n": 3}]  # the current turn keeps its data
    assert contents[1].parts[0].function_response.response["rows"]  # input not mutated


async def test_the_model_sees_compacted_history_on_later_turns(services) -> None:
    sql = "SELECT brand FROM products"
    chat = await open_chat(
        services, [call("run_query", sql=sql), text("a"), call("run_query", sql=sql), text("b")]
    )
    await chat.ask("first")
    await chat.ask("second")
    last = chat._runner.agent.model.requests[-1]
    responses = [
        p.function_response.response
        for c in last.contents
        for p in c.parts or []
        if p.function_response
    ]
    assert "rows" not in responses[0]
    assert "rows" in responses[1]


async def test_context_too_long_tells_the_user_to_start_over(services) -> None:
    too_long = genai_errors.ClientError(
        400,
        {
            "error": {
                "code": 400,
                "message": "The input token count (1200000) exceeds the maximum number of "
                "tokens allowed (1048576).",
            }
        },
    )
    chat = await open_chat(services, [too_long])
    turn = await chat.ask("hi")
    assert "/new" in turn.error
