"""Stage 4 evidence: run a few real questions through the agent (Gemini + BigQuery).

Prints each tool call, the tool status and the final answer with timings. Usage:

    uv run python scripts/smoke_agent.py [--user bob] ["question" ...]
"""

from __future__ import annotations

import argparse
import asyncio
import time

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from retail_agent.agent.agent import APP_NAME, build_agent
from retail_agent.agent.services import build_services
from retail_agent.config import Settings, load_env

QUESTIONS = [
    "What data can I analyse, and what period does it cover?",
    "Who are my top 5 customers by revenue in 2026, and how many orders did each place?",
    "Why does Levi's perform differently from Wrangler? Compare revenue, return rate and "
    "average sale price.",
    "Show monthly revenue for the last 6 full months.",
    "Which 5 states have the most churned customers?",
    "What is the email address of my top customer?",
    "How is Calvin Klein doing this year?",
]


async def main(user: str, questions: list[str]) -> None:
    load_env()
    services = build_services(Settings())
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=build_agent(services), session_service=sessions)
    session = await sessions.create_session(app_name=APP_NAME, user_id=user)
    for question in questions:
        print(f"\n=== {question}")
        started = time.monotonic()
        answer = []
        message = types.Content(role="user", parts=[types.Part(text=question)])
        async for event in runner.run_async(
            user_id=user, session_id=session.id, new_message=message
        ):
            for fc in event.get_function_calls():
                sql = " ".join(str(fc.args.get("sql", "")).split())
                print(f"  -> {fc.name} {sql[:200]}")
            for fr in event.get_function_responses():
                r = fr.response or {}
                detail = r.get("rule") or r.get("kind") or r.get("row_count", "")
                print(f"  <- {fr.name}: {r.get('status', '?')} {detail}")
            if event.content and event.is_final_response():
                answer.extend(p.text for p in event.content.parts or [] if p.text)
        print(f"  [{time.monotonic() - started:.1f}s]\n" + "\n".join(answer))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="bob")
    parser.add_argument("questions", nargs="*")
    args = parser.parse_args()
    asyncio.run(main(args.user, args.questions or QUESTIONS))
