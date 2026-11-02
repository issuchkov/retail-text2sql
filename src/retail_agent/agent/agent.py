"""The ADK agent: model, instruction, tools and the output guard."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from retail_agent.agent.instruction import build_instruction
from retail_agent.agent.redaction import redact
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


def build_agent(services: Services, model: str | BaseLlm | None = None) -> LlmAgent:
    return LlmAgent(
        name="retail_analyst",
        model=model or services.settings.model,
        instruction=build_instruction(services),
        tools=build_tools(services),
        after_model_callback=redact_model_output,
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
    )
