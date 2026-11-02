"""Golden bucket: trio format, scope-aware retrieval, the tool (SPEC §8 R1, AC-12)."""

from __future__ import annotations

import re

import pytest

from retail_agent.config import PROJECT_ROOT, Entitlement
from retail_agent.golden.library import GoldenLibrary, build_index, load_trios, visible_to
from retail_agent.query.guard import validate
from tests.unit.fakes import call, text
from tests.unit.test_cli import open_chat
from tests.unit.test_cli import services as services

TRIOS_DIR = PROJECT_ROOT / "golden" / "trios"
TRIOS = load_trios(TRIOS_DIR)
BOB = Entitlement(brands=["Levi's", "Wrangler", "Diesel"])
ALICE = Entitlement(brands=["Calvin Klein"])
CEO = Entitlement(all=True)
FIGURES = re.compile(r"[$€£]\s?\d|\d+(?:\.\d+)?\s?%|\b\d{3,}\b")


def test_sample_set_size_and_ids() -> None:
    assert 20 <= len(TRIOS) <= 30
    ids = [t.id for t in TRIOS]
    assert len(ids) == len(set(ids))
    assert {p.stem for p in TRIOS_DIR.glob("*.yaml")} == set(ids)


@pytest.mark.parametrize("trio", TRIOS, ids=lambda t: t.id)
def test_every_trio_sql_passes_the_guard(trio) -> None:
    validate(trio.sql, max_rows=1000)


@pytest.mark.parametrize("trio", TRIOS, ids=lambda t: t.id)
def test_reports_carry_no_figures(trio) -> None:
    assert not FIGURES.search(trio.report), trio.report


@pytest.mark.parametrize("trio", TRIOS, ids=lambda t: t.id)
def test_brand_specific_sql_declares_its_brands(trio) -> None:
    if re.search(r"brand\s*(=|IN\b)", trio.sql, re.IGNORECASE):
        assert trio.brands


def test_visibility_follows_scope() -> None:
    by_id = {t.id: t for t in TRIOS}
    levis, ck = by_id["brand-comparison-levis-wrangler"], by_id["calvin-klein-category-mix"]
    generic = by_id["monthly-revenue-trend"]
    assert visible_to(levis, BOB) and not visible_to(levis, ALICE)
    assert visible_to(ck, ALICE) and not visible_to(ck, BOB)
    assert all(visible_to(t, CEO) for t in TRIOS)
    assert visible_to(generic, ALICE) and visible_to(generic, BOB)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Which product categories sold best in the last quarter by revenue?",
            "top-categories-last-quarter",
        ),
        ("How many customers stopped buying from us?", "churned-customers"),
        ("Compare cancellation rates by quarter", "cancellation-rate-trend"),
        ("Write a quarterly report with action items", "quarter-report-with-actions"),
    ],
)
def test_lexical_search_finds_the_relevant_trio(question: str, expected: str) -> None:
    library = GoldenLibrary(TRIOS, index=None, embedder=None)
    result = library.search(question, CEO, k=3)
    assert result.method == "lexical"
    assert expected in [t.id for t, _ in result.hits]


def test_search_never_returns_out_of_scope_trios() -> None:
    library = GoldenLibrary(TRIOS, index=None, embedder=None)
    hits = library.search("Where does Calvin Klein make its revenue by category?", BOB, k=5).hits
    assert "calvin-klein-category-mix" not in [t.id for t, _ in hits]
    hits = library.search("Why does Levi's perform differently from Wrangler?", ALICE, k=5).hits
    assert "brand-comparison-levis-wrangler" not in [t.id for t, _ in hits]


class KeywordEmbedder:
    """Deterministic fake: one dimension per keyword."""

    KEYWORDS = ("churn", "revenue", "category", "return", "customer", "deliver", "margin")

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding service down")
        return [[float(k in t.lower()) for k in self.KEYWORDS] for t in texts]


def test_embedding_search_uses_the_index() -> None:
    embedder = KeywordEmbedder()
    index = build_index(TRIOS, embedder, model="fake", dims=7)
    library = GoldenLibrary(TRIOS, index=index, embedder=embedder, model="fake")
    result = library.search("What is our churn situation?", CEO, k=2)
    assert result.method == "embedding"
    assert result.hits[0][0].id in {"churned-customers", "churn-by-country"}


def test_embedding_failure_falls_back_to_lexical() -> None:
    index = build_index(TRIOS, KeywordEmbedder(), model="fake", dims=7)
    library = GoldenLibrary(TRIOS, index=index, embedder=KeywordEmbedder(fail=True), model="fake")
    result = library.search("How many customers stopped buying from us?", CEO, k=3)
    assert result.method == "lexical"
    assert "churned-customers" in [t.id for t, _ in result.hits]


def test_stale_index_is_not_used() -> None:
    index = build_index(TRIOS, KeywordEmbedder(), model="fake", dims=7)
    first = next(iter(index["entries"]))
    index["entries"][first]["text_hash"] = "changed"
    embedder = KeywordEmbedder()
    library = GoldenLibrary(TRIOS, index=index, embedder=embedder, model="fake")
    assert library.search("churn", CEO).method == "lexical"
    assert embedder.calls == 0


def test_shipped_index_matches_the_trios() -> None:
    library = GoldenLibrary.load(TRIOS_DIR, PROJECT_ROOT / "golden" / "index.json", embedder=None)
    assert library.index_is_fresh()


async def test_tool_returns_in_scope_examples(services) -> None:
    services.golden = GoldenLibrary(TRIOS, index=None, embedder=None)
    chat = await open_chat(
        services,
        [
            call(
                "find_similar_analyses", question="Why does Levi's sell differently from Wrangler?"
            ),
            text("ok"),
        ],
    )
    await chat.ask("why levi's vs wrangler?")
    (response,) = chat._runner.agent.model.function_responses()
    assert response["method"] == "lexical"
    ids = [e["id"] for e in response["examples"]]
    assert "brand-comparison-levis-wrangler" not in ids  # the test user only has Levi's
    assert all({"question", "sql", "report", "definitions"} <= set(e) for e in response["examples"])
    assert "not answers" in response["note"]
