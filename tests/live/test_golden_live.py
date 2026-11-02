"""Golden trios run on BigQuery; embedding retrieval finds them (SPEC §8 R1, AC-12)."""

import pytest

from retail_agent.config import PROJECT_ROOT, Entitlement
from retail_agent.golden.library import GeminiEmbedder, GoldenLibrary, load_trios
from retail_agent.query.guard import bind, validate

pytestmark = pytest.mark.live

TRIOS = load_trios(PROJECT_ROOT / "golden" / "trios")
CEO = Entitlement(all=True)


def scope_for(trio) -> Entitlement:
    return Entitlement(brands=trio.brands) if trio.brands else CEO


@pytest.mark.parametrize("trio", TRIOS, ids=lambda t: t.id)
def test_trio_sql_runs_and_returns_rows(executor, settings, trio) -> None:
    q = bind(
        validate(trio.sql, max_rows=1000), scope_for(trio), dataset=settings.dataset, salt="live"
    )
    result = executor.run(q, max_rows=1000)
    assert result.rows, f"{trio.id} returned no rows"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "Which kinds of products brought in the most money in the previous quarter?",
            "top-categories-last-quarter",
        ),
        ("How many shoppers have we lost recently?", "churned-customers"),
        ("Are deliveries taking longer than before?", "delivery-time-trend"),
        (
            "Which marketing channels bring customers who spend the most per order?",
            "traffic-source-order-value",
        ),
        (
            "Prepare a Q1 review with recommendations for next quarter",
            "quarter-report-with-actions",
        ),
    ],
)
def test_embedding_retrieval_finds_paraphrased_questions(question, expected) -> None:
    library = GoldenLibrary.load(
        PROJECT_ROOT / "golden" / "trios", PROJECT_ROOT / "golden" / "index.json", GeminiEmbedder()
    )
    result = library.search(question, CEO, k=3)
    assert result.method == "embedding"
    assert expected in [t.id for t, _ in result.hits]
