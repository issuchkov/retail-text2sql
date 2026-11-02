"""One user's chat with the agent, without terminal I/O (SPEC §3.3, §5.3, §6).

`ChatSession` runs turns through the ADK runner and turns its events into a `Turn`: the
answer, the SQL the model ran, a pending delete confirmation, or a short error message.
No exception escapes a turn; details go to the log under the turn's trace id.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.genai import errors as genai_errors
from google.genai import types

from retail_agent.agent.agent import APP_NAME, build_agent
from retail_agent.agent.resilience import ModelUnavailable
from retail_agent.agent.services import Services

log = logging.getLogger("retail_agent.cli")

CONFIRMATION_CALL = "adk_request_confirmation"


@dataclass(frozen=True)
class Confirmation:
    invocation_id: str
    call_id: str
    items: list[dict[str, Any]]
    phrase: str
    hint: str


@dataclass
class Turn:
    trace_id: str
    answer: str = ""
    queries: list[str] = field(default_factory=list)
    bytes_processed: int = 0
    confirmation: Confirmation | None = None
    error: str | None = None


def footer(turn: Turn) -> str:
    n = len(turn.queries)
    if not n:
        return f"trace {turn.trace_id} · no queries"
    label = "query" if n == 1 else "queries"
    return f"trace {turn.trace_id} · {n} {label} · {turn.bytes_processed / 1e6:.0f} MB"


def default_session_service(services: Services) -> BaseSessionService:
    path = services.settings.data_dir / "sessions.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{path}")


class ChatSession:
    def __init__(
        self, runner: Runner, sessions: BaseSessionService, user_id: str, session_id: str
    ) -> None:
        self._runner = runner
        self._sessions = sessions
        self.user_id = user_id
        self.session_id = session_id
        self.resumed = False
        self.last_turn: Turn | None = None

    @classmethod
    async def open(
        cls,
        services: Services,
        *,
        user_id: str,
        model: str | BaseLlm | None = None,
        session_service: BaseSessionService | None = None,
        resume: bool = True,
    ) -> ChatSession:
        services.user(user_id)  # fail early for an unknown identity
        sessions = session_service or default_session_service(services)
        runner = Runner(
            app_name=APP_NAME, agent=build_agent(services, model=model), session_service=sessions
        )
        existing = []
        if resume:
            listed = await sessions.list_sessions(app_name=APP_NAME, user_id=user_id)
            existing = sorted(listed.sessions, key=lambda s: s.last_update_time)
        if existing:
            chat = cls(runner, sessions, user_id, existing[-1].id)
            chat.resumed = True
            return chat
        created = await sessions.create_session(app_name=APP_NAME, user_id=user_id)
        return cls(runner, sessions, user_id, created.id)

    async def new_conversation(self) -> None:
        created = await self._sessions.create_session(app_name=APP_NAME, user_id=self.user_id)
        self.session_id = created.id
        self.resumed = False
        self.last_turn = None

    async def ask(self, text: str) -> Turn:
        message = types.Content(role="user", parts=[types.Part(text=text)])
        return await self._run(message)

    async def confirm(self, confirmation: Confirmation, typed: str) -> Turn:
        """Answer a pending delete confirmation with what the user typed.

        The tool checks the phrase itself; the CLI only passes the text through.
        """
        response = types.FunctionResponse(
            id=confirmation.call_id,
            name=CONFIRMATION_CALL,
            response={"confirmed": bool(typed.strip()), "payload": {"typed": typed}},
        )
        message = types.Content(role="user", parts=[types.Part(function_response=response)])
        return await self._run(message, invocation_id=confirmation.invocation_id)

    async def _run(self, message: types.Content, invocation_id: str | None = None) -> Turn:
        turn = Turn(trace_id=invocation_id or "")
        answer: list[str] = []
        try:
            async for event in self._runner.run_async(
                user_id=self.user_id,
                session_id=self.session_id,
                new_message=message,
                invocation_id=invocation_id,
            ):
                turn.trace_id = turn.trace_id or event.invocation_id
                _collect(event, turn)
                if event.content and event.is_final_response():
                    answer.extend(p.text for p in event.content.parts or [] if p.text)
        except Exception as e:
            turn.trace_id = turn.trace_id or f"t-{uuid.uuid4().hex[:12]}"
            log.exception("turn failed trace_id=%s", turn.trace_id)
            turn.error = f"{_friendly(e)} (trace {turn.trace_id})"
        turn.answer = "\n".join(answer).strip()
        self.last_turn = turn
        return turn


def _collect(event, turn: Turn) -> None:
    for call in event.get_function_calls():
        if call.name == "run_query":
            turn.queries.append(str((call.args or {}).get("sql", "")))
        elif call.name == CONFIRMATION_CALL:
            confirmation = (call.args or {}).get("toolConfirmation") or {}
            payload = confirmation.get("payload") or {}
            turn.confirmation = Confirmation(
                invocation_id=event.invocation_id,
                call_id=call.id,
                items=list(payload.get("items", [])),
                phrase=str(payload.get("phrase", "")),
                hint=str(confirmation.get("hint", "")),
            )
    for response in event.get_function_responses():
        if response.name == "run_query":
            turn.bytes_processed += int((response.response or {}).get("bytes_processed", 0))


def _friendly(error: Exception) -> str:
    if isinstance(error, genai_errors.ClientError) and getattr(error, "code", None) == 429:
        return "The model is rate-limited right now. Wait a minute and try again."
    if isinstance(error, (genai_errors.ServerError, ModelUnavailable)):
        return "The model service is unavailable right now. Try again shortly."
    return "Something went wrong while answering. Please try again."
