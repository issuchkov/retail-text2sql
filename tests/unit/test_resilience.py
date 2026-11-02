"""Model fallback, retries, circuit breaker and per-turn budgets (SPEC §6, AC-6, AC-7)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import PrivateAttr

from retail_agent.agent.resilience import ModelUnavailable, ResilientLlm
from retail_agent.query.executor import QueryFailed
from tests.unit.fakes import call, text
from tests.unit.test_cli import open_chat
from tests.unit.test_cli import services as services


def rate_limited(delay: str = "10s") -> genai_errors.ClientError:
    body = {
        "error": {
            "code": 429,
            "message": "quota",
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}],
        }
    }
    return genai_errors.ClientError(429, body)


def server_error() -> genai_errors.ServerError:
    return genai_errors.ServerError(503, {"error": {"code": 503, "message": "overloaded"}})


def bad_request() -> genai_errors.ClientError:
    return genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad schema"}})


class Inner(BaseLlm):
    """A model that plays a list of outcomes: an exception, "slow", or answer text."""

    _outcomes: list = PrivateAttr(default_factory=list)
    _seen_models: list = PrivateAttr(default_factory=list)

    def __init__(self, name: str, outcomes: list) -> None:
        super().__init__(model=name)
        self._outcomes = list(outcomes)

    @property
    def calls(self) -> int:
        return len(self._seen_models)

    @property
    def seen_models(self) -> list[str]:
        return self._seen_models

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._seen_models.append(llm_request.model)
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "slow":
            await asyncio.sleep(10)
        yield text(f"{self.model}: {outcome}")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(primary: Inner, fallback: Inner, clock: Clock | None = None):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    llm = ResilientLlm(
        [primary, fallback],
        timeout_s=0.05,
        max_wait_s=30,
        sleep=sleep,
        clock=clock or Clock(),
    )
    return llm, sleeps


async def answer(llm: ResilientLlm) -> str:
    request = LlmRequest(model="ignored", contents=[types.Content(role="user", parts=[])])
    responses = [r async for r in llm.generate_content_async(request)]
    return responses[0].content.parts[0].text


async def test_primary_answers_with_its_own_model_name() -> None:
    primary, fallback = Inner("p", ["ok"]), Inner("f", [])
    llm, _ = make(primary, fallback)
    assert await answer(llm) == "p: ok"
    assert primary.seen_models == ["p"]
    assert fallback.calls == 0
    assert llm.last_attempts == [{"model": "p", "outcome": "ok"}]


async def test_rate_limit_switches_to_fallback_without_waiting() -> None:
    primary, fallback = Inner("p", [rate_limited()]), Inner("f", ["ok"])
    llm, sleeps = make(primary, fallback)
    assert await answer(llm) == "f: ok"
    assert fallback.seen_models == ["f"]
    assert sleeps == []
    assert [a["outcome"] for a in llm.last_attempts] == ["rate_limited", "ok"]


async def test_timeout_switches_to_fallback() -> None:
    primary, fallback = Inner("p", ["slow"]), Inner("f", ["ok"])
    llm, _ = make(primary, fallback)
    assert await answer(llm) == "f: ok"
    assert llm.last_attempts[0]["outcome"] == "timeout"


async def test_server_error_is_retried_on_the_same_model_first() -> None:
    primary, fallback = Inner("p", [server_error(), "ok"]), Inner("f", [])
    llm, sleeps = make(primary, fallback)
    assert await answer(llm) == "p: ok"
    assert primary.calls == 2
    assert fallback.calls == 0
    assert len(sleeps) == 1


async def test_both_rate_limited_waits_for_retry_info_once() -> None:
    primary = Inner("p", [rate_limited("7s"), "ok"])
    fallback = Inner("f", [rate_limited("12s")])
    llm, sleeps = make(primary, fallback)
    assert await answer(llm) == "p: ok"
    assert sleeps == [7.0]  # the shortest advertised delay


async def test_rate_limit_wait_is_capped() -> None:
    primary = Inner("p", [rate_limited("120s"), "ok"])
    fallback = Inner("f", [rate_limited("90s")])
    llm, sleeps = make(primary, fallback)
    with pytest.raises(ModelUnavailable):
        await answer(llm)
    assert sleeps == []  # waiting longer than max_wait_s is not worth it in a chat


async def test_everything_failing_raises_model_unavailable() -> None:
    primary = Inner("p", [server_error(), server_error(), server_error()])
    fallback = Inner("f", [server_error(), server_error(), server_error()])
    llm, _ = make(primary, fallback)
    with pytest.raises(ModelUnavailable):
        await answer(llm)


async def test_bad_request_is_not_retried_or_hidden() -> None:
    primary, fallback = Inner("p", [bad_request()]), Inner("f", ["ok"])
    llm, _ = make(primary, fallback)
    with pytest.raises(genai_errors.ClientError):
        await answer(llm)
    assert fallback.calls == 0


async def test_circuit_breaker_skips_a_failing_model_until_cooldown() -> None:
    clock = Clock()
    primary = Inner("p", [rate_limited()] * 3 + ["ok"])
    fallback = Inner("f", ["ok"] * 5)
    llm, _ = make(primary, fallback, clock)
    for _ in range(3):
        await answer(llm)
    assert primary.calls == 3
    assert await answer(llm) == "f: ok"  # breaker open: primary not even tried
    assert primary.calls == 3
    clock.now += 61
    assert await answer(llm) == "p: ok"  # half-open after the cooldown


# --- per-turn budgets through the real runner -----------------------------------------


async def test_failed_queries_are_capped_per_turn(services) -> None:
    services.executor.error = QueryFailed("invalid_query", "Unrecognized name: x")
    script = [call("run_query", sql=f"SELECT x{i} FROM orders") for i in range(4)]
    chat = await open_chat(services, [*script, text("I could not answer.")])
    await chat.ask("q")
    assert len(services.executor.queries) == 3
    responses = chat._runner.agent.model.function_responses()
    assert responses[-1]["status"] == "budget_exhausted"
    assert "3" in responses[-1]["message"]


async def test_guard_rejections_count_as_failed_attempts(services) -> None:
    script = [call("run_query", sql="SELECT email FROM users") for _ in range(4)]
    chat = await open_chat(services, [*script, text("stop")])
    await chat.ask("q")
    responses = chat._runner.agent.model.function_responses()
    assert [r["status"] for r in responses] == ["rejected"] * 3 + ["budget_exhausted"]


async def test_queries_are_capped_per_turn(services) -> None:
    services.settings.max_queries_per_turn = 2
    script = [call("run_query", sql="SELECT brand FROM products") for _ in range(3)]
    chat = await open_chat(services, [*script, text("enough")])
    await chat.ask("q")
    assert len(services.executor.queries) == 2
    responses = chat._runner.agent.model.function_responses()
    assert responses[-1]["status"] == "budget_exhausted"


async def test_budgets_reset_on_the_next_turn(services) -> None:
    services.settings.max_queries_per_turn = 1
    sql = "SELECT brand FROM products"
    chat = await open_chat(
        services, [call("run_query", sql=sql), text("a"), call("run_query", sql=sql), text("b")]
    )
    await chat.ask("one")
    await chat.ask("two")
    assert len(services.executor.queries) == 2


async def test_model_calls_are_capped_per_turn(services) -> None:
    services.settings.max_model_calls_per_turn = 3
    script = [call("list_reports") for _ in range(10)]
    chat = await open_chat(services, script)
    turn = await chat.ask("loop forever")
    assert len(chat._runner.agent.model.requests) == 3
    assert "limit" in turn.answer.lower()


async def test_model_unavailable_is_a_friendly_message(services) -> None:
    chat = await open_chat(services, [ModelUnavailable("all models failed")])
    turn = await chat.ask("hi")
    assert turn.error is not None
    assert "unavailable" in turn.error.lower()


async def test_retired_model_falls_back() -> None:
    retired = genai_errors.ClientError(
        404, {"error": {"code": 404, "message": "model is no longer available"}}
    )
    primary, fallback = Inner("p", [retired]), Inner("f", ["ok"])
    llm, _ = make(primary, fallback)
    assert await answer(llm) == "f: ok"
    assert llm.last_attempts[0]["outcome"] == "not_found"
