"""Golden bucket: analysts' Question → SQL → Report trios as examples (SPEC §8 R1).

Trios live in `golden/trios/*.yaml`. `find_similar_analyses` returns the closest trios the
user is allowed to see: a trio about specific brands is shown only to users whose scope
covers them, and reports carry no figures, so examples never leak out-of-scope numbers.

Ranking uses embeddings precomputed into `golden/index.json`
(`scripts/build_golden_index.py`); if the index is stale or embedding the question fails,
a local TF-IDF ranking over the same texts is used instead.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field

from retail_agent.config import Entitlement

log = logging.getLogger("retail_agent.golden")

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMS = 256
# Minimum similarity for an example to be returned. Calibrated on gemini-embedding-001:
# off-topic questions scored at most 0.671 against the trios, the weakest on-topic match
# 0.698; lexical TF-IDF scored 0.0 for every off-topic question.
MIN_EMBEDDING_SIMILARITY = 0.70
MIN_LEXICAL_SIMILARITY = 0.10
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "by",
        "do",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "the",
        "this",
        "to",
        "we",
        "what",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
        "my",
        "me",
        "i",
    }
)


class Definition(BaseModel):
    term: str
    definition: str


class Trio(BaseModel):
    id: str
    question: str
    intent: str
    sql: str
    report: str
    definitions: list[Definition] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    author: str = ""
    status: str = "seed"
    reviewed_at: dt.date | None = None

    def search_text(self) -> str:
        return f"{self.question}\n{self.intent}\n{', '.join(self.tags)}"

    def text_hash(self) -> str:
        return hashlib.sha256(self.search_text().encode("utf-8")).hexdigest()[:16]

    def as_example(self, similarity: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "intent": self.intent,
            "sql": self.sql,
            "report": self.report,
            "definitions": [d.model_dump() for d in self.definitions],
            "similarity": round(similarity, 3),
        }


class Embedder(Protocol):
    def embed(self, texts: list[str], task: str) -> list[list[float]]: ...


class GeminiEmbedder:
    def __init__(self, model: str = EMBEDDING_MODEL, dims: int = EMBEDDING_DIMS) -> None:
        from google import genai

        self.model = model
        self.dims = dims
        self._client = genai.Client()

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        from google.genai import types

        response = self._client.models.embed_content(
            model=self.model,
            contents=texts,
            config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dims),
        )
        return [list(e.values or []) for e in response.embeddings or []]


@dataclass(frozen=True)
class SearchResult:
    method: str  # "embedding" | "lexical"
    hits: list[tuple[Trio, float]]


def load_trios(directory: Path) -> list[Trio]:
    trios = []
    for path in sorted(directory.glob("*.yaml")):
        trio = Trio.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if trio.id != path.stem:
            raise ValueError(f"{path.name}: id {trio.id!r} must match the file name")
        trios.append(trio)
    return trios


def visible_to(trio: Trio, scope: Entitlement) -> bool:
    return scope.all or set(trio.brands) <= set(scope.brands)


def build_index(trios: list[Trio], embedder: Embedder, *, model: str, dims: int) -> dict:
    vectors = embedder.embed([t.search_text() for t in trios], "RETRIEVAL_DOCUMENT")
    return {
        "model": model,
        "dims": dims,
        "entries": {
            t.id: {"text_hash": t.text_hash(), "vector": v}
            for t, v in zip(trios, vectors, strict=True)
        },
    }


class GoldenLibrary:
    def __init__(
        self,
        trios: list[Trio],
        *,
        index: dict | None,
        embedder: Embedder | None,
        model: str = EMBEDDING_MODEL,
    ) -> None:
        self.trios = trios
        self._index = index
        self._embedder = embedder
        self._model = model
        self._lexical = _Tfidf([t.search_text() for t in trios])

    @classmethod
    def load(cls, directory: Path, index_path: Path, embedder: Embedder | None) -> GoldenLibrary:
        index = None
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
        return cls(load_trios(directory), index=index, embedder=embedder)

    def index_is_fresh(self) -> bool:
        if not self._index or self._index.get("model") != self._model:
            return False
        entries = self._index.get("entries", {})
        return all(
            t.id in entries and entries[t.id].get("text_hash") == t.text_hash() for t in self.trios
        )

    def search(self, question: str, scope: Entitlement, k: int = 3) -> SearchResult:
        allowed = [
            i for i, t in enumerate(self.trios) if t.status != "retired" and visible_to(t, scope)
        ]
        if self._embedder is not None:
            if self.index_is_fresh():
                try:
                    (query,) = self._embedder.embed([question], "RETRIEVAL_QUERY")
                    entries = self._index["entries"]  # type: ignore[index]
                    scores = {
                        i: _cosine(query, entries[self.trios[i].id]["vector"]) for i in allowed
                    }
                    return SearchResult("embedding", self._top(scores, k, MIN_EMBEDDING_SIMILARITY))
                except Exception:
                    log.warning("embedding search failed; using lexical ranking", exc_info=True)
            else:
                log.warning(
                    "golden index is stale or built with another model; using lexical ranking "
                    "(run scripts/build_golden_index.py)"
                )
        scores = {i: s for i, s in self._lexical.scores(question).items() if i in allowed}
        return SearchResult("lexical", self._top(scores, k, MIN_LEXICAL_SIMILARITY))

    def _top(self, scores: dict[int, float], k: int, minimum: float) -> list[tuple[Trio, float]]:
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [(self.trios[i], score) for i, score in ranked[:k] if score >= minimum]


class _Tfidf:
    def __init__(self, documents: list[str]) -> None:
        tokenized = [_tokens(d) for d in documents]
        df = Counter(term for tokens in tokenized for term in set(tokens))
        n = len(documents)
        self._idf = {term: math.log((1 + n) / (1 + count)) + 1 for term, count in df.items()}
        self._vectors = [self._vector(tokens) for tokens in tokenized]

    def scores(self, query: str) -> dict[int, float]:
        q = self._vector(_tokens(query))
        return {i: _sparse_cosine(q, v) for i, v in enumerate(self._vectors)}

    def _vector(self, tokens: list[str]) -> dict[str, float]:
        counts = Counter(t for t in tokens if t in self._idf)
        return {term: count * self._idf[term] for term, count in counts.items()}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return [_stem(w) for w in words if w not in _STOPWORDS]


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _sparse_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(value * b.get(term, 0.0) for term, value in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0
