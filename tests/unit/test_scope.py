"""Binding logical tables to scoped, PII-free CTEs (SPEC §4.3, AC-2, AC-3)."""

import sqlglot
from sqlglot import exp

from retail_agent.config import Entitlement
from retail_agent.query.guard import bind, validate
from retail_agent.query.schema import PII_COLUMNS

DATASET = "bigquery-public-data.thelook_ecommerce"
SALT = "test-salt"
BOB = Entitlement(categories=["Outerwear & Coats", "Jeans"])
ALICE = Entitlement(departments=["Women"])
CAROL = Entitlement(all=True)


def prepare(sql: str, scope: Entitlement):
    return bind(validate(sql, max_rows=1000), scope, dataset=DATASET, salt=SALT)


def cte_names(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read="bigquery")
    return [cte.alias for cte in tree.args["with_"].expressions]


def base_tables(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql, read="bigquery")
    return {t.name for t in tree.find_all(exp.Table) if t.args.get("db")}


def test_only_needed_ctes_in_dependency_order() -> None:
    assert cte_names(prepare("SELECT COUNT(*) FROM products", BOB).sql) == ["products"]
    assert cte_names(prepare("SELECT COUNT(*) FROM order_items", BOB).sql) == [
        "products",
        "order_items",
    ]
    assert cte_names(prepare("SELECT COUNT(*) FROM orders", BOB).sql) == [
        "products",
        "order_items",
        "orders",
    ]
    assert cte_names(prepare("SELECT COUNT(*) FROM users", BOB).sql) == ["products", "users"]


def test_model_ctes_come_after_scoped_ctes() -> None:
    q = prepare(
        "WITH m AS (SELECT status, COUNT(*) AS n FROM orders GROUP BY status) SELECT * FROM m",
        BOB,
    )
    assert cte_names(q.sql) == ["products", "order_items", "orders", "m"]


def test_scope_filters_are_parameters_not_literals() -> None:
    q = prepare("SELECT category FROM products", BOB)
    assert "@scope_categories" in q.sql
    assert "Outerwear" not in q.sql  # values never spliced into SQL text
    params = {p.name: p for p in q.params}
    assert params["scope_categories"].value == ["Outerwear & Coats", "Jeans"]
    assert params["scope_categories"].type == "STRING"
    assert params["scope_categories"].array
    assert "scope_departments" not in params


def test_department_scope() -> None:
    q = prepare("SELECT category FROM products", ALICE)
    assert "@scope_departments" in q.sql
    assert {p.name for p in q.params} == {"scope_departments"}


def test_unrestricted_scope_has_no_product_filter() -> None:
    q = prepare("SELECT category FROM products", CAROL)
    assert "@scope_" not in q.sql
    assert q.params == []


def test_customer_ids_are_pseudonymised_with_salt_parameter() -> None:
    q = prepare("SELECT customer_key FROM orders", BOB)
    assert "@pseudonym_salt" in q.sql
    salt = next(p for p in q.params if p.name == "pseudonym_salt")
    assert salt.value == SALT
    assert SALT not in q.sql


def test_bound_sql_never_selects_pii() -> None:
    q = prepare("SELECT * FROM users JOIN orders USING (customer_key)", CAROL)
    columns = {
        c.name.lower() for c in sqlglot.parse_one(q.sql, read="bigquery").find_all(exp.Column)
    }
    assert not columns & PII_COLUMNS


def test_base_tables_only_appear_inside_scoped_ctes() -> None:
    q = prepare("SELECT * FROM users JOIN orders USING (customer_key)", BOB)
    assert base_tables(q.sql) <= {"products", "order_items", "orders", "users"}
    tree = sqlglot.parse_one(q.sql, read="bigquery")
    outer = tree.copy()
    outer.set("with_", None)
    assert not [t for t in outer.find_all(exp.Table) if t.args.get("db")]


def test_limit_is_in_bound_sql() -> None:
    q = prepare("SELECT category FROM products", BOB)
    assert sqlglot.parse_one(q.sql, read="bigquery").args["limit"].expression.this == "1000"


def test_bound_sql_parses_as_bigquery() -> None:
    q = prepare(
        "WITH m AS (SELECT DATE_TRUNC(created_at, MONTH) AS month, SUM(order_revenue) AS rev "
        "FROM orders GROUP BY month) SELECT * FROM m ORDER BY month",
        ALICE,
    )
    sqlglot.parse_one(q.sql, read="bigquery")


def test_logical_table_names_are_normalised_to_scoped_ctes() -> None:
    q = prepare("SELECT status, COUNT(*) FROM Orders GROUP BY status", BOB)
    tree = sqlglot.parse_one(q.sql, read="bigquery")
    outer = tree.copy()
    outer.set("with_", None)
    assert [t.name for t in outer.find_all(exp.Table)] == ["orders"]
