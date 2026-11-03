"""Reproducible fault injection against the real model (SPEC §6, AC-7).

Runs one question through the chat session three times and prints which models were tried:
  normal                 — the primary answers
  retired primary        — RETAIL_MODEL points at a model that does not exist (404) → fallback
  timeout                — a 0.01 s per-call timeout → both models time out → friendly error

    uv run python scripts/fault_injection.py [--user bob]

Uses a few model calls and one BigQuery query per scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from google.adk.sessions import InMemorySessionService

from retail_agent.agent.services import build_services
from retail_agent.cli.chat import ChatSession
from retail_agent.config import Settings, load_env

QUESTION = "How many orders did my brands have last month? One sentence."
SCENARIOS = {
    "normal": {},
    "retired primary": {"RETAIL_MODEL": "gemini-retired-for-fault-injection"},
    "timeout": {"RETAIL_MODEL_TIMEOUT_S": 0.01},
}


async def main(user: str) -> None:
    load_env()
    logging.disable(logging.CRITICAL)  # the chat session reports failures itself
    for name, overrides in SCENARIOS.items():
        services = build_services(Settings(**overrides))
        chat = await ChatSession.open(
            services, user_id=user, session_service=InMemorySessionService(), resume=False
        )
        turn = await chat.ask(QUESTION)
        attempts = chat._runner.agent.model.last_attempts
        print(f"== {name}")
        print(f"   attempts: {[(a['model'], a['outcome']) for a in attempts]}")
        print(f"   answer:   {turn.answer[:160] or '-'}")
        print(f"   error:    {turn.error or '-'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="bob")
    asyncio.run(main(parser.parse_args().user))
