"""Model calls that survive the free tier: fallback, retries, timeouts (SPEC §6, ADR-0003).

`ResilientLlm` wraps the primary and fallback models behind one ADK model:

- a rate limit (429) switches to the next model at once — each model has its own quota;
- a timeout switches to the next model (free-tier latency swings by 10x within an hour);
- a 404 (a retired or misspelled model) switches to the next model;
- a server or network error is retried once on the same model, with backoff and jitter;
- when every model is rate-limited, it waits once for the shortest advertised `RetryInfo`
  delay if that is short enough for a chat, then tries again;
- a circuit breaker per model skips a model after repeated failures until a cooldown;
- any other client error (a bad request) is raised unchanged: retrying cannot fix it.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors
from pydantic import PrivateAttr


class ModelUnavailable(Exception):
    """Every model failed or was rate-limited for longer than a chat can wait."""


@dataclass
class CircuitBreaker:
    threshold: int = 3
    cooldown_s: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    def available(self, now: float) -> bool:
        return self.opened_at is None or now - self.opened_at >= self.cooldown_s

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = now


@dataclass
class _Policy:
    timeout_s: float
    max_wait_s: float
    server_retries: int
    sleep: Callable[[float], Awaitable[None]]
    clock: Callable[[], float]


class ResilientLlm(BaseLlm):
    _models: list[BaseLlm] = PrivateAttr(default_factory=list)
    _breakers: list[CircuitBreaker] = PrivateAttr(default_factory=list)
    _policy: _Policy | None = PrivateAttr(default=None)
    _last_attempts: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    def __init__(
        self,
        models: list[BaseLlm],
        *,
        timeout_s: float = 25.0,
        max_wait_s: float = 30.0,
        server_retries: int = 1,
        breaker_threshold: int = 3,
        cooldown_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(model=models[0].model)
        self._models = list(models)
        self._breakers = [CircuitBreaker(breaker_threshold, cooldown_s) for _ in models]
        self._policy = _Policy(timeout_s, max_wait_s, server_retries, sleep, clock)

    @property
    def last_attempts(self) -> list[dict[str, Any]]:
        """What happened on the last call: one entry per attempt (for tracing)."""
        return list(self._last_attempts)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        for response in await self._generate(llm_request):
            yield response

    async def _generate(self, llm_request: LlmRequest) -> list[LlmResponse]:
        policy = self._policy
        assert policy is not None
        self._last_attempts = []
        for round_ in range(2):
            rate_delays: list[float] = []
            for model, breaker in zip(self._models, self._breakers, strict=True):
                if not breaker.available(policy.clock()):
                    self._record(model, "skipped_open_circuit")
                    continue
                for attempt in range(1 + policy.server_retries):
                    try:
                        responses = await asyncio.wait_for(
                            self._call(model, llm_request), timeout=policy.timeout_s
                        )
                    except Exception as e:
                        kind = _classify(e)
                        if kind == "fatal":
                            raise
                        breaker.failure(policy.clock())
                        self._record(model, kind)
                        if kind == "rate_limited":
                            delay = _retry_delay(e)
                            if delay is not None:
                                rate_delays.append(delay)
                            break
                        if kind in ("timeout", "not_found") or attempt == policy.server_retries:
                            break
                        await policy.sleep(min(4.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))
                        continue
                    breaker.success()
                    self._record(model, "ok")
                    return responses
            if round_ == 0 and rate_delays and min(rate_delays) <= policy.max_wait_s:
                await policy.sleep(min(rate_delays))
                continue
            break
        summary = ", ".join(f"{a['model']}: {a['outcome']}" for a in self._last_attempts)
        raise ModelUnavailable(summary or "no model available")

    async def _call(self, model: BaseLlm, llm_request: LlmRequest) -> list[LlmResponse]:
        request = llm_request.model_copy(deep=True)
        request.model = model.model
        return [r async for r in model.generate_content_async(request, stream=False)]

    def _record(self, model: BaseLlm, outcome: str) -> None:
        self._last_attempts.append({"model": model.model, "outcome": outcome})


def _classify(error: Exception) -> str:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, genai_errors.ClientError):
        return {429: "rate_limited", 404: "not_found"}.get(error.code, "fatal")
    if isinstance(error, genai_errors.ServerError):
        return "server_error"
    if isinstance(error, httpx.TransportError):
        return "network"
    return "fatal"


def _retry_delay(error: Exception) -> float | None:
    """The `RetryInfo.retryDelay` a 429 response advertises, in seconds."""
    details = getattr(error, "details", None)
    if not isinstance(details, dict):
        return None
    for item in (details.get("error") or {}).get("details") or []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            match = re.fullmatch(r"\s*([\d.]+)s\s*", str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None
