"""Persona (tone) without redeploy (SPEC §8 R8).

`config/persona.md` is the shipped default. A persona editor's change from the chat is
written to the runtime file (`data/persona.md`), the replaced text is kept in the history
directory, and the instruction picks it up on the next turn. The persona only sets tone:
rules, scope and definitions are added by code and take precedence over it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersonaVersion:
    replaced_at: str
    author: str  # who replaced it
    text: str


class PersonaStore:
    MAX_CHARS = 4000

    def __init__(self, default_path: Path, active_path: Path, history_dir: Path) -> None:
        self.default_path = default_path
        self.active_path = active_path
        self.history_dir = history_dir

    def read(self) -> str:
        for path in (self.active_path, self.default_path):
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        return ""

    def version(self) -> str:
        """Short hash of the persona in effect, recorded in every turn's trace."""
        return hashlib.sha256(self.read().encode("utf-8")).hexdigest()[:12]

    def set(self, text: str, *, author: str) -> None:
        new = text.strip()[: self.MAX_CHARS]
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.history_dir.mkdir(parents=True, exist_ok=True)
        safe_author = re.sub(r"[^A-Za-z0-9_-]", "_", author)
        (self.history_dir / f"{stamp}-{safe_author}.md").write_text(self.read(), encoding="utf-8")
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.active_path.with_suffix(".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(self.active_path)

    def history(self) -> list[PersonaVersion]:
        if not self.history_dir.exists():
            return []
        versions = []
        for path in sorted(self.history_dir.glob("*.md"), reverse=True):
            stamp, _, author = path.stem.partition("-")
            versions.append(PersonaVersion(stamp, author, path.read_text(encoding="utf-8")))
        return versions
