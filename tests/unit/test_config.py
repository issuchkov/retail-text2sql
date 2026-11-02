"""Identities and brand scopes (SPEC §2, §4.3)."""

import pytest
from pydantic import ValidationError

from retail_agent.config import PROJECT_ROOT, Entitlement, load_users


def test_shipped_users_file_loads() -> None:
    users = load_users(PROJECT_ROOT / "config" / "users.yaml")
    assert users["alice"].scope.brands == ["Calvin Klein"]
    assert len(users["bob"].scope.brands) > 1
    assert users["carol"].scope.all


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"all": False},
        {"brands": []},
        {"all": True, "brands": ["Levi's"]},
        {"categories": ["Jeans"]},  # scopes are brands only
        {"brands": ["Levi's"], "departments": ["Women"]},
    ],
)
def test_scope_is_all_or_a_brand_list(data: dict) -> None:
    with pytest.raises(ValidationError):
        Entitlement.model_validate(data)


def test_describe() -> None:
    assert Entitlement(brands=["Levi's", "Wrangler"]).describe() == "brands: Levi's, Wrangler"
    assert Entitlement(all=True).describe() == "all brands"
