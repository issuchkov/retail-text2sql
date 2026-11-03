"""Figures in answers must come from query results (SPEC §4.5 grounded figures)."""

import pytest

from retail_agent.agent.grounding import grounding_numbers, ungrounded_figures

RESULTS = [
    {
        "status": "ok",
        "rows": [
            {"brand": "Levi's", "net_revenue": 21603.85, "orders": 412},
            {"brand": "Wrangler", "net_revenue": 13222.40, "orders": 301},
            {"brand": "Diesel", "net_revenue": 17150.76, "orders": 120},
        ],
        "row_count": 3,
    },
]


@pytest.mark.parametrize(
    "answer",
    [
        "Levi's made $21,603.85 and Wrangler $13,222.40.",
        "Levi's leads with about $21.6k.",
        "Together the portfolio made $51,977.01.",  # column total
        "Levi's and Diesel together: $38,754.61.",  # sum of two values
        "Levi's made $8,381 more than Wrangler.",  # difference of two values
        "Levi's share is 41.6% and it had 412 orders.",  # percentages and small counts
        "Since 2019 the data covers every year up to 2026.",  # years are not figures
    ],
)
def test_grounded_answers_pass(answer: str) -> None:
    assert ungrounded_figures(answer, grounding_numbers(RESULTS, [])) == []


def test_invented_figures_are_flagged() -> None:
    answer = "Your 2025 net revenue was $144,369,637.38, with Levi's at $72,551,643.08."
    assert ungrounded_figures(answer, grounding_numbers(RESULTS, [])) == [
        144369637.38,
        72551643.08,
    ]


def test_without_any_query_every_figure_is_unverified() -> None:
    assert ungrounded_figures("Revenue was $51,977.", grounding_numbers([], [])) == [51977.0]


def test_numbers_from_the_users_messages_count_as_grounded() -> None:
    numbers = grounding_numbers([], ["Our target is 25,000 orders this year"])
    assert ungrounded_figures("You aim for 25,000 orders.", numbers) == []


def test_numeric_strings_in_rows_count() -> None:
    numbers = grounding_numbers([{"rows": [{"first_order": "2019-01-20", "orders": "1234"}]}], [])
    assert ungrounded_figures("There are 1,234 orders.", numbers) == []


def test_describe_data_coverage_counts() -> None:
    numbers = grounding_numbers([{"coverage": {"orders": 4136, "first_order": "2019-01-20"}}], [])
    assert ungrounded_figures("You have 4,136 orders.", numbers) == []
