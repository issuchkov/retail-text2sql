"""Print the full correspondence of a turn or a session (SPEC §7 deep dive).

    uv run python scripts/show_conversation.py <trace_id | session_id>

The trace id is the one in the CLI footer and in logs/traces.jsonl. Reads the local
session store (data/sessions.sqlite).
"""

from __future__ import annotations

import asyncio
import sys

from google.adk.sessions import DatabaseSessionService

from retail_agent.agent.agent import APP_NAME
from retail_agent.config import Settings, load_env, load_users
from retail_agent.observability.conversation import render_turns


async def main(key: str) -> int:
    load_env()
    settings = Settings()
    sessions = DatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{settings.data_dir / 'sessions.sqlite'}"
    )
    for user_id in load_users(settings.users_file):
        listed = await sessions.list_sessions(app_name=APP_NAME, user_id=user_id)
        for meta in listed.sessions:
            session = await sessions.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=meta.id
            )
            if session is None:
                continue
            if session.id == key:
                print(f"session {session.id} · user {user_id}\n")
                print(render_turns(session.events))
                return 0
            if any(e.invocation_id == key for e in session.events):
                print(f"session {session.id} · user {user_id}\n")
                print(render_turns(session.events, trace_id=key))
                return 0
    print(f"No session or trace {key!r} in {settings.data_dir / 'sessions.sqlite'}")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
