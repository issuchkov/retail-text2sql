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
from retail_agent.agent.instruction import build_instruction
from retail_agent.agent.redaction import redact
from retail_agent.agent.resilience import ResilientLlm
from retail_agent.agent.services import Services
from retail_agent.agent.tools import build_tools

APP_NAME = "retail_agent"
REDACTIONS_KEY = "temp:redactions"


def redact_model_output(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Redact PII patterns from model text before it is shown or stored (SPEC §4.5)."""
    content = llm_response.content
    if not content or not content.parts:
        return None
    found: list[str] = []
    parts = []
    for part in content.parts:
        if part.text:
            cleaned, kinds = redact(part.text)
            found.extend(kinds)
            part = part.model_copy(update={"text": cleaned})
        parts.append(part)
    if not found:
        return None
    callback_context.state[REDACTIONS_KEY] = list(dict.fromkeys(found))
    return llm_response.model_copy(
        update={"content": types.Content(role=content.role, parts=parts)}
    )


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
        before_model_callback=model_budget(services),
        after_model_callback=redact_model_output,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )
