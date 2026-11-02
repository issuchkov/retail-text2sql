"""Settings from the environment and executive identities from `config/users.yaml`."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Export `.env` into the process so Google client libraries see their variables."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)


class Settings(BaseSettings):
    """Runtime configuration. Secrets come only from the environment or `.env`."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    google_cloud_project: str | None = None
    dataset: str = "bigquery-public-data.thelook_ecommerce"
    bq_location: str = "US"

    model: str = Field("gemini-3.5-flash-lite", alias="RETAIL_MODEL")
    fallback_model: str = Field("gemini-3.1-flash-lite", alias="RETAIL_FALLBACK_MODEL")

    max_bytes_billed: int = Field(1_000_000_000, alias="RETAIL_MAX_BYTES_BILLED")
    max_result_rows: int = Field(1_000, alias="RETAIL_MAX_RESULT_ROWS")
    query_timeout_s: float = Field(60.0, alias="RETAIL_QUERY_TIMEOUT_S")
    pseudonym_salt: str | None = Field(None, alias="RETAIL_PSEUDONYM_SALT")

    users_file: Path = Field(PROJECT_ROOT / "config" / "users.yaml", alias="RETAIL_USERS_FILE")
    persona_file: Path = Field(PROJECT_ROOT / "config" / "persona.md", alias="RETAIL_PERSONA_FILE")
    data_dir: Path = Field(PROJECT_ROOT / "data", alias="RETAIL_DATA_DIR")
    log_dir: Path = Field(PROJECT_ROOT / "logs", alias="RETAIL_LOG_DIR")


class Entitlement(BaseModel):
    """Brands a user may analyse (SPEC §4.3): a brand list, or `all: true` (e.g. the CEO)."""

    model_config = ConfigDict(extra="forbid")

    all: bool = False
    brands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> Entitlement:
        if self.all == bool(self.brands):
            raise ValueError("scope must be either `all: true` or a non-empty `brands` list")
        return self

    def describe(self) -> str:
        return "all brands" if self.all else f"brands: {', '.join(self.brands)}"


class UserProfile(BaseModel):
    id: str
    name: str
    title: str = ""
    scope: Entitlement


def load_users(path: Path) -> dict[str, UserProfile]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users = [UserProfile.model_validate(item) for item in raw.get("users", [])]
    by_id = {u.id: u for u in users}
    if len(by_id) != len(users):
        raise ValueError(f"duplicate user ids in {path}")
    return by_id
