"""Logical schema exposed to the model (SPEC §4.2, §4.3).

The model writes SQL against these four names. The server binds each one to a scoped,
PII-free CTE over the base table (`scope.py`), so the columns below are the only ones that
exist from the model's point of view.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    description: str


LOGICAL_TABLES: dict[str, tuple[Column, ...]] = {
    "products": (
        Column("id", "INTEGER", "Product id"),
        Column("cost", "FLOAT", "Unit cost to the company"),
        Column("category", "STRING", "Product category, e.g. 'Jeans', 'Outerwear & Coats'"),
        Column("name", "STRING", "Product name"),
        Column("brand", "STRING", "Brand name"),
        Column("retail_price", "FLOAT", "List price"),
        Column("department", "STRING", "'Men' or 'Women'"),
        Column("sku", "STRING", "Stock keeping unit"),
        Column("distribution_center_id", "INTEGER", "Distribution center id"),
    ),
    "order_items": (
        Column("id", "INTEGER", "Order item id"),
        Column("order_id", "INTEGER", "Order id (joins orders.order_id)"),
        Column("customer_key", "STRING", "Pseudonymous customer id (joins users.customer_key)"),
        Column("product_id", "INTEGER", "Product id (joins products.id)"),
        Column("inventory_item_id", "INTEGER", "Inventory item id"),
        Column(
            "status",
            "STRING",
            "Item status: Processing, Shipped, Complete, Cancelled, Returned",
        ),
        Column("created_at", "TIMESTAMP", "When the item was ordered"),
        Column("shipped_at", "TIMESTAMP", "When it was shipped"),
        Column("delivered_at", "TIMESTAMP", "When it was delivered"),
        Column("returned_at", "TIMESTAMP", "When it was returned"),
        Column("sale_price", "FLOAT", "Price paid for the item (revenue)"),
    ),
    "orders": (
        Column("order_id", "INTEGER", "Order id"),
        Column("customer_key", "STRING", "Pseudonymous customer id (joins users.customer_key)"),
        Column(
            "status",
            "STRING",
            "Order status: Processing, Shipped, Complete, Cancelled, Returned",
        ),
        Column("gender", "STRING", "Customer gender, 'M' or 'F'"),
        Column("created_at", "TIMESTAMP", "When the order was placed"),
        Column("returned_at", "TIMESTAMP", "When it was returned"),
        Column("shipped_at", "TIMESTAMP", "When it was shipped"),
        Column("delivered_at", "TIMESTAMP", "When it was delivered"),
        Column("num_of_item", "INTEGER", "Number of items in the order within your scope"),
        Column("order_revenue", "FLOAT", "Sum of sale_price of the order's items in your scope"),
    ),
    "users": (
        Column("customer_key", "STRING", "Pseudonymous customer id"),
        Column("age", "INTEGER", "Age in years"),
        Column("gender", "STRING", "'M' or 'F'"),
        Column("state", "STRING", "State or region"),
        Column("city", "STRING", "City"),
        Column("country", "STRING", "Country"),
        Column("traffic_source", "STRING", "Acquisition channel, e.g. Search, Organic, Email"),
        Column("created_at", "TIMESTAMP", "When the customer account was created"),
    ),
}

# Columns of the base `users` table that are personal data and never exposed (SPEC §4.2).
PII_COLUMNS = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "street_address",
        "postal_code",
        "latitude",
        "longitude",
        "user_geom",
    }
)

# Raw customer identifiers, replaced by `customer_key`.
RAW_CUSTOMER_ID_COLUMNS = frozenset({"user_id"})


def describe_schema() -> str:
    """Plain-text schema for the model's instructions and the `describe_data` tool."""
    lines = []
    for table, columns in LOGICAL_TABLES.items():
        lines.append(f"{table}:")
        lines.extend(f"  - {c.name} ({c.type}): {c.description}" for c in columns)
    return "\n".join(lines)
