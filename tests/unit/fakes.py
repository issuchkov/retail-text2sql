"""Test doubles: a scripted ADK model and an in-memory query executor."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from retail_agent.query.executor import QueryFailed, QueryResult
from retail_agent.query.guard import PreparedQuery


def call(name: str, **args: Any) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]
        )
    )


def text(value: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=value)]))


class ScriptedLlm(BaseLlm):
    """Returns the scripted responses in order and records every request."""

    model: str = "scripted"
    _script: list[LlmResponse] = PrivateAttr(default_factory=list)
    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

    def __init__(self, script: list[LlmResponse]) -> None:
        super().__init__()
        self._script = list(script)

    @property
    def requests(self) -> list[LlmRequest]:
        return self._requests

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._requests.append(llm_request)
        yield self._script.pop(0) if self._script else text("(script exhausted)")

    def function_responses(self) -> list[dict[str, Any]]:
        """All function responses the model has been sent, in order."""
        seen: list[dict[str, Any]] = []
        for request in self._requests:
            for content in request.contents:
                for part in content.parts or []:
                    if part.function_response and part.function_response.response not in seen:
                        seen.append(part.function_response.response)
        return seen


class FakeExecutor:
    def __init__(self, rows=None, error: QueryFailed | None = None) -> None:
        self.rows = rows if rows is not None else [{"brand": "Levi's", "revenue": 10.0}]
        self.error = error
        self.queries: list[PreparedQuery] = []

    def run(self, query: PreparedQuery, *, max_rows: int) -> QueryResult:
        self.queries.append(query)
        if self.error:
            raise self.error
        rows = self.rows[:max_rows]
        return QueryResult(
            sql=query.sql,
            columns=list(rows[0]) if rows else [],
            rows=rows,
            truncated=len(rows) >= max_rows,
            bytes_processed=1_000_000,
            bytes_billed=10_000_000,
            duration_ms=5,
        )

    def dry_run(self, query: PreparedQuery) -> int:
        return 1_000_000
