"""What the agent's tools run against: settings, BigQuery, the report store, identities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from retail_agent.agent.definitions import DefinitionStore
from retail_agent.agent.persona import PersonaStore
from retail_agent.agent.preferences import PreferenceStore
from retail_agent.config import PROJECT_ROOT, Settings, UserProfile
from retail_agent.golden.library import GoldenLibrary
from retail_agent.query.executor import QueryResult
from retail_agent.query.guard import PreparedQuery
from retail_agent.reports.store import ReportStore


class QueryRunner(Protocol):
    def run(self, query: PreparedQuery, *, max_rows: int) -> QueryResult: ...

    def dry_run(self, query: PreparedQuery) -> int: ...


class UnknownUser(Exception):
    pass


@dataclass
class Services:
    settings: Settings
    executor: QueryRunner
    store: ReportStore
    users: dict[str, UserProfile]
    definitions: DefinitionStore = field(default_factory=lambda: DefinitionStore(":memory:"))
    preferences: PreferenceStore = field(default_factory=lambda: PreferenceStore(":memory:"))
    persona: PersonaStore | None = None
    golden: GoldenLibrary | None = None
    coverage_cache: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.settings.pseudonym_salt:
            raise ValueError("RETAIL_PSEUDONYM_SALT must be set (see .env.example)")
        if self.persona is None:
            self.persona = PersonaStore(
                self.settings.persona_file,
                self.settings.persona_override_file,
                self.settings.persona_history_dir,
            )

    def user(self, user_id: str) -> UserProfile:
        """The identity of a session. The id comes from the session, never from the model."""
        try:
            return self.users[user_id]
        except KeyError:
            raise UnknownUser(user_id) from None


def build_services(settings: Settings) -> Services:
    """Real dependencies: BigQuery, the SQLite report store and configured identities."""
    from retail_agent.config import load_users
    from retail_agent.golden.library import GeminiEmbedder
    from retail_agent.query.executor import make_executor

    golden_dir = PROJECT_ROOT / "golden"
    return Services(
        settings=settings,
        executor=make_executor(settings),
        store=ReportStore(settings.data_dir / "reports.sqlite"),
        users=load_users(settings.users_file),
        definitions=DefinitionStore(settings.data_dir / "preferences.sqlite"),
        preferences=PreferenceStore(settings.data_dir / "preferences.sqlite"),
        golden=GoldenLibrary.load(
            golden_dir / "trios", golden_dir / "index.json", embedder=GeminiEmbedder()
        ),
    )
