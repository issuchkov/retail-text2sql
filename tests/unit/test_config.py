"""Identities and entitlements (SPEC §2, §4.3)."""

import pytest
from pydantic import ValidationError

from retail_agent.config import PROJECT_ROOT, Entitlement, load_users


def test_shipped_users_file_loads() -> None:
    users = load_users(PROJECT_ROOT / "config" / "users.yaml")
    assert {"alice", "bob", "carol"} <= users.keys()
    assert users["carol"].scope.all


@pytest.mark.parametrize(
    "data",
    [{}, {"all": True, "categories": ["Jeans"]}, {"all": False}],
)
def test_entitlement_must_be_all_or_filtered(data: dict) -> None:
    with pytest.raises(ValidationError):
        Entitlement.model_validate(data)


def test_describe() -> None:
    e = Entitlement(departments=["Women"], brands=["Levi's"])
    assert e.describe() == "departments: Women; brands: Levi's"
    assert Entitlement(all=True).describe() == "all products"
