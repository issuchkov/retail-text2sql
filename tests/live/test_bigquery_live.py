"""Scoping and PII rules hold on the real dataset (SPEC §4.2, §4.3). Run: make test-live."""

import re

import pytest

from retail_agent.config import Entitlement
from retail_agent.query.guard import bind, validate
from tests.unit.test_guard import ACCEPTED_DEMOGRAPHIC_QUERIES

pytestmark = pytest.mark.live

BOB = Entitlement(brands=["Levi's", "Wrangler", "Diesel"])
ALICE = Entitlement(brands=["Calvin Klein"])
CAROL = Entitlement(all=True)


def run(executor, settings, sql, scope):
    q = bind(validate(sql, max_rows=1000), scope, dataset=settings.dataset, salt="live-test")
    return executor.run(q, max_rows=1000)


def test_products_are_limited_to_brand_scope(executor, settings) -> None:
    r = run(executor, settings, "SELECT DISTINCT brand FROM products", BOB)
    assert {row["brand"] for row in r.rows} == {"Levi's", "Wrangler", "Diesel"}
    r = run(executor, settings, "SELECT DISTINCT brand FROM products", ALICE)
    assert {row["brand"] for row in r.rows} == {"Calvin Klein"}


def test_out_of_scope_items_are_invisible(executor, settings) -> None:
    r = run(
        executor,
        settings,
        "SELECT COUNT(*) AS n FROM order_items AS oi JOIN products AS p ON p.id = oi.product_id "
        "WHERE p.brand NOT IN (\"Levi's\", 'Wrangler', 'Diesel')",
        BOB,
    )
    assert r.rows == [{"n": 0}]


def test_order_revenue_is_net_of_cancelled_and_returned_items(executor, settings) -> None:
    sql = (
        "SELECT (SELECT ROUND(SUM(order_revenue), 2) FROM orders) AS from_orders, "
        "(SELECT ROUND(SUM(IF(status NOT IN ('Cancelled', 'Returned'), sale_price, 0)), 2) "
        "FROM order_items) AS net_from_items, "
        "(SELECT ROUND(SUM(sale_price), 2) FROM order_items) AS gross_from_items, "
        "(SELECT SUM(num_of_item) FROM orders) AS items_in_orders, "
        "(SELECT COUNT(*) FROM order_items) AS items"
    )
    r = run(executor, settings, sql, BOB).rows[0]
    assert r["from_orders"] == pytest.approx(r["net_from_items"])
    assert r["from_orders"] < r["gross_from_items"]
    assert r["items_in_orders"] == r["items"]


def test_customer_keys_are_pseudonyms_and_join(executor, settings) -> None:
    r = run(
        executor,
        settings,
        "SELECT u.customer_key, COUNT(*) AS orders FROM orders AS o "
        "JOIN users AS u USING (customer_key) GROUP BY 1 ORDER BY orders DESC LIMIT 5",
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


@pytest.mark.parametrize("sql", ACCEPTED_DEMOGRAPHIC_QUERIES)
def test_queries_the_guard_accepts_run_on_bigquery(executor, settings, sql) -> None:
    result = run(executor, settings, sql, BOB)
    assert result.bytes_processed < settings.max_bytes_billed
