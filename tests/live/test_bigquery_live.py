"""Scoping and PII rules hold on the real dataset (SPEC §4.2, §4.3). Run: make test-live."""

import re

import pytest

from retail_agent.config import Entitlement
from retail_agent.query.guard import bind, validate

pytestmark = pytest.mark.live

BOB = Entitlement(categories=["Outerwear & Coats", "Jeans"])
ALICE = Entitlement(departments=["Women"])
CAROL = Entitlement(all=True)


def run(executor, settings, sql, scope):
    q = bind(validate(sql, max_rows=1000), scope, dataset=settings.dataset, salt="live-test")
    return executor.run(q, max_rows=1000)


def test_products_are_limited_to_entitlement(executor, settings) -> None:
    r = run(executor, settings, "SELECT DISTINCT category FROM products", BOB)
    assert {row["category"] for row in r.rows} == {"Outerwear & Coats", "Jeans"}
    r = run(executor, settings, "SELECT DISTINCT department FROM products", ALICE)
    assert {row["department"] for row in r.rows} == {"Women"}


def test_out_of_scope_items_are_invisible(executor, settings) -> None:
    r = run(
        executor,
        settings,
        "SELECT COUNT(*) AS n FROM order_items AS oi JOIN products AS p ON p.id = oi.product_id "
        "WHERE p.category NOT IN ('Outerwear & Coats', 'Jeans')",
        BOB,
    )
    assert r.rows == [{"n": 0}]


def test_order_totals_are_recomputed_over_scope(executor, settings) -> None:
    sql = (
        "SELECT (SELECT ROUND(SUM(order_revenue), 2) FROM orders) AS from_orders, "
        "(SELECT ROUND(SUM(sale_price), 2) FROM order_items) AS from_items, "
        "(SELECT SUM(num_of_item) FROM orders) AS items_in_orders, "
        "(SELECT COUNT(*) FROM order_items) AS items"
    )
    r = run(executor, settings, sql, BOB).rows[0]
    assert r["from_orders"] == pytest.approx(r["from_items"])
    assert r["items_in_orders"] == r["items"]


def test_customer_keys_are_pseudonyms_and_join(executor, settings) -> None:
    r = run(
        executor,
        settings,
        "SELECT u.customer_key, u.state, COUNT(*) AS orders FROM orders AS o "
        "JOIN users AS u USING (customer_key) GROUP BY 1, 2 ORDER BY orders DESC LIMIT 5",
        BOB,
    )
    assert r.rows
    assert all(re.fullmatch(r"[0-9a-f]{16}", row["customer_key"]) for row in r.rows)


def test_scoped_users_are_buyers_of_entitled_products(executor, settings) -> None:
    scoped = run(executor, settings, "SELECT COUNT(*) AS n FROM users", BOB).rows[0]["n"]
    everyone = run(executor, settings, "SELECT COUNT(*) AS n FROM users", CAROL).rows[0]["n"]
    assert 0 < scoped < everyone


def test_typical_query_is_well_under_the_byte_cap(executor, settings) -> None:
    q = bind(
        validate(
            "SELECT DATE_TRUNC(DATE(o.created_at), MONTH) AS month, u.country, "
            "SUM(o.order_revenue) AS revenue FROM orders AS o JOIN users AS u "
            "USING (customer_key) GROUP BY 1, 2",
            max_rows=1000,
        ),
        ALICE,
        dataset=settings.dataset,
        salt="live-test",
    )
    estimate = executor.dry_run(q)
    print(f"dry-run estimate: {estimate / 1e6:.1f} MB")
    assert estimate < settings.max_bytes_billed / 10
