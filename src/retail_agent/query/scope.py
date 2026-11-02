"""Scoped, PII-free CTEs over the base tables (SPEC §4.2, §4.3).

Each logical table becomes a CTE that only contains the products of the user's brands and
the rows derived from them, with PII columns dropped and customer ids replaced by a salted
hash. Brand values and the salt are BigQuery query parameters.
"""

from __future__ import annotations

from dataclasses import dataclass

from retail_agent.config import Entitlement


@dataclass(frozen=True)
class QueryParam:
    name: str
    type: str
    value: object
    array: bool = False


# Logical table -> logical tables its CTE reads from. Order of this dict is the CTE order.
_DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "products": (),
    "order_items": ("products",),
    "orders": ("order_items",),
    "users": ("products",),
}


def _customer_key(column: str) -> str:
    return f"SUBSTR(TO_HEX(SHA256(CONCAT(@pseudonym_salt, CAST({column} AS STRING)))), 1, 16)"


def _bodies(dataset: str, product_filter: str) -> dict[str, str]:
    base = f"`{dataset}"
    return {
        "products": (
            "SELECT id, cost, category, name, brand, retail_price, department, sku, "
            f"distribution_center_id FROM {base}.products` WHERE {product_filter}"
        ),
        "order_items": (
            f"SELECT oi.id, oi.order_id, {_customer_key('oi.user_id')} AS customer_key, "
            "oi.product_id, oi.inventory_item_id, oi.status, oi.created_at, oi.shipped_at, "
            f"oi.delivered_at, oi.returned_at, oi.sale_price FROM {base}.order_items` AS oi "
            "WHERE oi.product_id IN (SELECT id FROM products)"
        ),
        "orders": (
            f"SELECT o.order_id, {_customer_key('o.user_id')} AS customer_key, o.status, "
            "o.created_at, o.returned_at, o.shipped_at, o.delivered_at, "
            "s.num_of_item, s.order_revenue "
            f"FROM {base}.orders` AS o JOIN (SELECT order_id, COUNT(*) AS num_of_item, "
            "SUM(IF(status IN ('Cancelled', 'Returned'), 0, sale_price)) AS order_revenue "
            "FROM order_items GROUP BY order_id) AS s "
            "ON s.order_id = o.order_id"
        ),
        "users": (
            f"SELECT {_customer_key('u.id')} AS customer_key, u.age, u.gender, u.state, "
            f"u.city, u.country, u.traffic_source, u.created_at FROM {base}.users` AS u"
        ),
    }


def _users_filter(dataset: str) -> str:
    return (
        f" WHERE u.id IN (SELECT user_id FROM `{dataset}.order_items` "
        "WHERE product_id IN (SELECT id FROM products))"
    )


def _needed(tables: set[str]) -> list[str]:
    needed: set[str] = set()
    stack = list(tables)
    while stack:
        name = stack.pop()
        if name not in needed:
            needed.add(name)
            stack.extend(_DEPENDS_ON[name])
    return [name for name in _DEPENDS_ON if name in needed]


def scoped_ctes(
    tables: set[str], scope: Entitlement, *, dataset: str, salt: str
) -> tuple[list[tuple[str, str]], list[QueryParam]]:
    params: list[QueryParam] = []
    product_filter = "TRUE"
    if not scope.all:
        product_filter = "brand IN UNNEST(@scope_brands)"
        params.append(QueryParam("scope_brands", "STRING", list(scope.brands), array=True))

    bodies = _bodies(dataset, product_filter)
    if not scope.all:
        bodies["users"] += _users_filter(dataset)

    names = _needed(tables)
    if set(names) - {"products"}:
        params.append(QueryParam("pseudonym_salt", "STRING", salt))
    return [(name, bodies[name]) for name in names], params
