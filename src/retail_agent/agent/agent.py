"""The ADK agent: model, instruction, tools and the output guard."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import Gemini
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from retail_agent.agent import budget
from retail_agent.agent.context import compact_before_model
from retail_agent.agent.grounding import grounding_numbers, ungrounded_figures
from retail_agent.agent.instruction import build_instruction
from retail_agent.agent.redaction import redact
from retail_agent.agent.resilience import ResilientLlm
from retail_agent.agent.services import Services
from retail_agent.agent.tools import build_tools

APP_NAME = "retail_agent"
REDACTIONS_KEY = "temp:redactions"
UNGROUNDED_KEY = "temp:ungrounded"


def guard_model_output(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Output guard (SPEC §4.5): redact PII patterns; flag figures no query produced.

    One callback, because ADK stops at the first after-model callback that returns a
    response.
    """
    content = llm_response.content
    if not content or not content.parts:
        return None
    changed = False
    found: list[str] = []
    parts = []
    for part in content.parts:
        if part.text:
            cleaned, kinds = redact(part.text)
            if kinds:
                found.extend(kinds)
                changed = True
                part = part.model_copy(update={"text": cleaned})
        parts.append(part)
    if found:
        callback_context.state[REDACTIONS_KEY] = list(dict.fromkeys(found))

    if not any(p.function_call for p in parts):  # the final answer of a step
        answer = "".join(p.text or "" for p in parts)
        results, user_texts = _evidence(callback_context.session.events)
        flagged = ungrounded_figures(answer, grounding_numbers(results, user_texts))
        if flagged:
            callback_context.state[UNGROUNDED_KEY] = flagged
            listed = ", ".join(f"{n:,.2f}".removesuffix(".00") for n in flagged)
            note = (
                f"\n\n> **Unverified figures:** {listed} could not be traced to a query "
                "result in this conversation. Ask me to recompute them before relying on them."
            )
            parts.append(types.Part(text=note))
            changed = True
    if not changed:
        return None
    return llm_response.model_copy(
        update={"content": types.Content(role=content.role, parts=parts)}
    )


def _evidence(events) -> tuple[list[dict], list[str]]:
    """Tool results and the user's own words in the conversation so far."""
    results: list[dict] = []
    user_texts: list[str] = []
    for event in events:
        for response in event.get_function_responses():
            if isinstance(response.response, dict):
                results.append(response.response)
        if event.author == "user" and event.content:
            user_texts.extend(p.text for p in event.content.parts or [] if p.text)
    return results, user_texts


def model_budget(services: Services):
    """Stop a turn that needs more model calls than its budget (SPEC §6)."""

    def before_model(
        callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        limit = services.settings.max_model_calls_per_turn
        spent = budget.counters(callback_context.state, callback_context.invocation_id)
        spent["model_calls"] += 1
        budget.save(callback_context.state, spent)
        if spent["model_calls"] <= limit:
            return None
        message = (
            f"I reached the limit of {limit} steps for one question before finishing. "
            "Please narrow the question or split it into smaller ones."
        )
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=message)]))

    return before_model


def resilient_model(services: Services) -> ResilientLlm:
    settings = services.settings
    no_retries = types.HttpRetryOptions(attempts=1)  # ResilientLlm owns retries
    return ResilientLlm(
        [
            Gemini(model=settings.model, retry_options=no_retries),
            Gemini(model=settings.fallback_model, retry_options=no_retries),
        ],
        timeout_s=settings.model_timeout_s,
        max_wait_s=settings.model_max_wait_s,
    )


def build_agent(services: Services, model: str | BaseLlm | None = None) -> LlmAgent:
    return LlmAgent(
        name="retail_analyst",
        model=model or resilient_model(services),
        instruction=build_instruction(services),
        tools=build_tools(services),
        before_model_callback=[model_budget(services), compact_before_model],
        after_model_callback=guard_model_output,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )
