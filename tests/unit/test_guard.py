"""Query guard: what the model may run (SPEC §4.4, AC-4)."""

import pytest

from retail_agent.query.guard import QueryRejected, validate

MAX_ROWS = 1000


@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        ("", "syntax"),
        ("SELEC category FROM products", "syntax"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT 1; DELETE FROM orders WHERE TRUE", "multiple_statements"),
        ("DELETE FROM orders WHERE TRUE", "not_select"),
        ("INSERT INTO orders (order_id) VALUES (1)", "not_select"),
        ("UPDATE orders SET status = 'x' WHERE TRUE", "not_select"),
        ("DROP TABLE orders", "not_select"),
        ("CREATE TABLE t AS SELECT * FROM orders", "not_select"),
        ("DECLARE x INT64", "not_select"),
        ("EXPORT DATA OPTIONS (uri = 'gs://b/*', format = 'CSV') AS SELECT 1", "not_select"),
        ("CALL some_proc()", "not_select"),
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`", "qualified_table"),
        ("SELECT * FROM thelook_ecommerce.orders", "qualified_table"),
        ("SELECT * FROM INFORMATION_SCHEMA.TABLES", "qualified_table"),
        (
            "SELECT * FROM orders WHERE customer_key IN "
            "(SELECT CAST(id AS STRING) FROM `bigquery-public-data.thelook_ecommerce.users`)",
            "qualified_table",
        ),
        ("SELECT * FROM inventory_items", "unknown_table"),
        ("SELECT * FROM `orders_*`", "unknown_table"),
        ("SELECT email FROM users", "pii_column"),
        ("SELECT EMAIL FROM USERS", "pii_column"),
        ("SELECT u.first_name, u.last_name FROM users AS u", "pii_column"),
        ("SELECT ST_ASTEXT(user_geom) FROM users", "pii_column"),
        ("SELECT state FROM users WHERE postal_code = '10001'", "pii_column"),
        ("SELECT COUNT(DISTINCT user_id) FROM orders", "raw_customer_id"),
        ("WITH users AS (SELECT 1 AS x) SELECT * FROM users", "shadowed_table"),
        ("SELECT SESSION_USER()", "forbidden_function"),
        ("SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')", "forbidden_function"),
        ("SELECT * FROM ML.PREDICT(MODEL m, (SELECT 1 AS x))", "forbidden_function"),
        ("SELECT * FROM orders WHERE status = @status", "parameter"),
        ("SELECT * FROM orders WHERE status = ?", "parameter"),
    ],
)
def test_rejects(sql: str, rule: str) -> None:
    with pytest.raises(QueryRejected) as err:
        validate(sql, max_rows=MAX_ROWS)
    assert err.value.rule == rule
    assert err.value.message  # an actionable explanation for self-correction


def test_accepts_join_and_adds_limit() -> None:
    q = validate(
        "SELECT p.category, SUM(oi.sale_price) AS revenue "
        "FROM order_items AS oi JOIN products AS p ON p.id = oi.product_id "
        "GROUP BY p.category ORDER BY revenue DESC",
        max_rows=MAX_ROWS,
    )
    assert q.tables == {"order_items", "products"}
    assert q.limit == MAX_ROWS


def test_model_cte_is_allowed_and_tracked_by_its_sources() -> None:
    q = validate(
        "WITH monthly AS (SELECT DATE_TRUNC(created_at, MONTH) AS month, COUNT(*) AS n "
        "FROM orders GROUP BY month) SELECT * FROM monthly ORDER BY month",
        max_rows=MAX_ROWS,
    )
    assert q.tables == {"orders"}


@pytest.mark.parametrize(("given", "expected"), [(50, 50), (1000, 1000), (100000, 1000)])
def test_limit_kept_or_capped(given: int, expected: int) -> None:
    q = validate(f"SELECT category FROM products LIMIT {given}", max_rows=MAX_ROWS)
    assert q.limit == expected


def test_union_gets_limit() -> None:
    q = validate(
        "SELECT 'M' AS g, COUNT(*) AS n FROM users WHERE gender = 'M' "
        "UNION ALL SELECT 'F', COUNT(*) FROM users WHERE gender = 'F'",
        max_rows=MAX_ROWS,
    )
    assert q.tables == {"users"}
    assert q.limit == MAX_ROWS


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM products",  # products.id is not a customer id
        "SELECT state, COUNT(*) AS customers FROM users GROUP BY state",
        "SELECT x FROM UNNEST([1, 2, 3]) AS x",
        "select status, count(*) from Orders group by status",
        "SELECT customer_key, SUM(order_revenue) AS spend FROM orders GROUP BY customer_key",
    ],
)
def test_accepts_safe_queries(sql: str) -> None:
    validate(sql, max_rows=MAX_ROWS)
