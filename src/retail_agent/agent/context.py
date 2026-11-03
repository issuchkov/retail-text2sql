"""Bounded conversations (SPEC §6 conversation length).

Every model call resends the conversation. Tool results from earlier turns — query rows,
golden examples, report bodies — are the bulk of it and are no longer needed: the answers
written from them are still in the history. Before each call, results of earlier turns are
compacted to their metadata; the current turn keeps its data. The session itself is not
changed, so the full correspondence stays available for debugging.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

_KEEP = (
    "status",
    "rule",
    "kind",
    "row_count",
    "columns",
    "row_limit_reached",
    "method",
    "id",
    "title",
    "count",
    "deleted_ids",
    "term",
    "aspect",
    "value",
    "scope",
)
NOTE = "compacted: data from an earlier turn; run the query again if you need the rows"


def compact_history(contents: list[types.Content]) -> list[types.Content]:
    """A copy of `contents` with function responses before the current turn compacted."""
    start = _current_turn_start(contents)
    compacted = []
    for index, content in enumerate(contents):
        if index >= start or not any(p.function_response for p in content.parts or []):
            compacted.append(content)
            continue
        parts = []
        for part in content.parts or []:
            response = part.function_response
            if response is not None and isinstance(response.response, dict):
                part = part.model_copy(
                    update={
                        "function_response": response.model_copy(
                            update={"response": _summary(response.response)}
                        )
                    }
                )
            parts.append(part)
        compacted.append(content.model_copy(update={"parts": parts}))
    return compacted


def compact_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    llm_request.contents = compact_history(list(llm_request.contents))
    return None


def _current_turn_start(contents: list[types.Content]) -> int:
    """Index of the last user message that is text (the question of the current turn)."""
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        if content.role == "user" and any(p.text for p in content.parts or []):
            return index
    return 0


def _summary(response: dict[str, Any]) -> dict[str, Any]:
    kept = {k: response[k] for k in _KEEP if k in response}
    kept["note"] = NOTE
    return kept
