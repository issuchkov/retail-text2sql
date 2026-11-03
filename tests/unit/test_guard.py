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


# --- demographics only in aggregates (SPEC §4.2, §4.4, AC-10) ---------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_key, city FROM users",
        "SELECT customer_key, city AS c FROM users",
        "SELECT city FROM users",  # one row per customer
        "SELECT * FROM users",
        "SELECT u FROM users AS u",  # whole row as a struct
        "WITH t AS (SELECT customer_key, state FROM users) SELECT * FROM t",
        "SELECT x.s FROM (SELECT state AS s, customer_key FROM users) AS x",
        "SELECT customer_key, city, COUNT(*) AS n FROM users GROUP BY customer_key, city",
        "SELECT DISTINCT customer_key, state FROM users",
        "SELECT o.order_id, u.age FROM orders AS o JOIN users AS u USING (customer_key) "
        "GROUP BY 1, 2",
        "SELECT oi.id, u.gender FROM order_items AS oi JOIN users AS u USING (customer_key)",
        "SELECT customer_key, MAX(age) AS age FROM users GROUP BY customer_key",
        "SELECT city, ARRAY_AGG(customer_key) AS customers FROM users GROUP BY city",
        "SELECT city, MAX(customer_key) AS someone FROM users GROUP BY city",
        "SELECT ARRAY_AGG(STRUCT(customer_key, city)) AS rows FROM users",
        "SELECT customer_key, COUNT(*) OVER (PARTITION BY state) AS n FROM users",
        "SELECT u.customer_key, t.city FROM users AS u "
        "JOIN (SELECT city, COUNT(*) AS n FROM users GROUP BY city) AS t USING (city)",
        "SELECT city FROM users GROUP BY city UNION ALL SELECT city FROM users",
        "SELECT CONCAT(customer_key, '-', country) AS k FROM users",
        "SELECT o.customer_key, (SELECT u.city FROM users AS u "
        "WHERE u.customer_key = o.customer_key) AS c FROM orders AS o",
        "SELECT customer_key, TO_JSON_STRING(u) AS j FROM users AS u",
        "SELECT customer_key FROM users UNION ALL SELECT city FROM users",
        "SELECT customer_key, RANK() OVER (ORDER BY age) AS r FROM users",
        "SELECT state, customer_key, COUNT(*) AS n FROM users "
        "GROUP BY ROLLUP (state, customer_key)",
        # value subqueries are checked on their own
        "SELECT ARRAY(SELECT AS STRUCT customer_key, city FROM users) AS everyone",
        "SELECT (SELECT city FROM users WHERE customer_key = 'abc') AS c",
        # a group pinned to specific customers is not an aggregate
        "SELECT city, COUNT(*) AS n FROM users WHERE customer_key = 'abc' GROUP BY city",
        "SELECT state, COUNT(*) AS n FROM users WHERE customer_key IN ('a', 'b') GROUP BY state",
        "SELECT u.age, COUNT(*) AS n FROM orders AS o JOIN users AS u USING (customer_key) "
        "WHERE o.order_id = 42 GROUP BY 1",
    ],
)
def test_rejects_demographics_of_individuals(sql: str) -> None:
    with pytest.raises(QueryRejected) as err:
        validate(sql, max_rows=MAX_ROWS)
    assert err.value.rule == "demographics_not_aggregated"
    assert "aggregate" in err.value.message


# Legal analytical queries; tests/live runs every one of them on BigQuery.
ACCEPTED_DEMOGRAPHIC_QUERIES = [
    "SELECT city, COUNT(*) AS customers FROM users GROUP BY city",
    "SELECT state, COUNT(DISTINCT customer_key) AS buyers FROM users GROUP BY state",
    "SELECT AVG(age) AS avg_age FROM users",
    "SELECT DISTINCT country FROM users",
    "SELECT state, COUNT(*) AS n FROM users GROUP BY state HAVING n > 5 ORDER BY n DESC",
    "WITH t AS (SELECT state, COUNT(*) AS n FROM users GROUP BY state) "
    "SELECT * FROM t ORDER BY n DESC",
    "SELECT age_band, COUNT(*) AS n FROM (SELECT CASE WHEN age < 30 THEN 'under 30' "
    "ELSE '30+' END AS age_band FROM users) GROUP BY age_band",
    "SELECT u.traffic_source, DATE_TRUNC(DATE(o.created_at), MONTH) AS month, "
    "SUM(o.order_revenue) AS revenue FROM orders AS o JOIN users AS u USING (customer_key) "
    "GROUP BY 1, 2",
    "SELECT city, ARRAY_AGG(DISTINCT traffic_source) AS sources FROM users GROUP BY city",
    # customer-level analysis without demographics in the output
    "SELECT customer_key, SUM(order_revenue) AS revenue FROM orders GROUP BY customer_key",
    "SELECT * FROM orders",
    "SELECT o.customer_key, SUM(o.order_revenue) AS revenue FROM orders AS o "
    "JOIN users AS u USING (customer_key) WHERE u.state = 'California' GROUP BY 1",
    "SELECT p.category, COUNT(*) AS items FROM order_items AS oi "
    "JOIN products AS p ON p.id = oi.product_id GROUP BY 1",
    "SELECT customer_key FROM users ORDER BY age",
    "SELECT (SELECT COUNT(*) FROM users WHERE city = 'Austin') AS n",
    "SELECT o.customer_key, SUM(o.order_revenue) AS revenue FROM orders AS o "
    "WHERE o.customer_key = 'abc' GROUP BY 1",  # pinned, but no demographics output
    "SELECT state, COUNT(*) AS n FROM users WHERE customer_key IN "
    "(SELECT customer_key FROM orders WHERE order_revenue > 100) GROUP BY state",
]


@pytest.mark.parametrize("sql", ACCEPTED_DEMOGRAPHIC_QUERIES)
def test_accepts_aggregated_demographics_and_customer_level_analysis(sql: str) -> None:
    validate(sql, max_rows=MAX_ROWS)


def test_standard_sql_quote_escaping_gets_a_bigquery_hint() -> None:
    with pytest.raises(QueryRejected) as err:
        validate("SELECT * FROM products WHERE brand = 'Levi''s'", max_rows=MAX_ROWS)
    assert err.value.rule == "syntax"
    assert '"Levi\'s"' in err.value.message


# --- identifiers may not single out customers when demographics are output -------------
# An independent review found these bypasses of the earlier "pinning" rule.

KEY = "'3fa2b9c41d0e7a55'"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT city, age, gender, COUNT(*) AS n FROM users "
        f"WHERE STARTS_WITH(customer_key, {KEY}) GROUP BY city, age, gender",
        "SELECT city, COUNT(*) AS n FROM users "
        f"WHERE customer_key IN UNNEST([{KEY}]) GROUP BY city",
        f"SELECT city, COUNT(*) AS n FROM users WHERE customer_key BETWEEN {KEY} AND {KEY} "
        "GROUP BY city",
        "SELECT city, COUNT(*) AS n FROM users WHERE REGEXP_CONTAINS(customer_key, r'^3fa2') "
        "GROUP BY city",
        f"WITH t AS (SELECT {KEY} AS k) SELECT u.city, COUNT(*) AS n FROM users AS u "
        "JOIN t ON u.customer_key = t.k GROUP BY 1",
        f"SELECT city, state, COUNTIF(customer_key = {KEY}) AS hit FROM users GROUP BY 1, 2",
        f"SELECT city, SUM(IF(customer_key = {KEY}, 1, 0)) AS hit FROM users GROUP BY 1",
        "SELECT city, COUNT(*) AS n FROM users WHERE SUBSTR(customer_key, 1, 4) = '3fa2' "
        "GROUP BY city",
        f"SELECT city, COUNT(*) AS n FROM users WHERE CAST(customer_key AS STRING) = {KEY} "
        "GROUP BY city",
        f"SELECT city, COUNT(*) AS n FROM users WHERE customer_key > {KEY} GROUP BY city",
        f"SELECT u.state, COUNT(*) AS n FROM users AS u WHERE u.customer_key IN "
        f"(SELECT customer_key FROM users WHERE STARTS_WITH(customer_key, {KEY})) GROUP BY 1",
        "SELECT u.age, COUNT(*) AS n FROM orders AS o JOIN users AS u USING (customer_key) "
        "WHERE o.order_id BETWEEN 42 AND 42 GROUP BY 1",
    ],
)
def test_rejects_identifiers_that_single_out_a_customer(sql: str) -> None:
    with pytest.raises(QueryRejected) as err:
        validate(sql, max_rows=MAX_ROWS)
    assert err.value.rule == "demographics_not_aggregated"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT city, COUNT(*) AS n FROM users WHERE customer_key IN "
        "(SELECT customer_key FROM orders ORDER BY order_revenue DESC LIMIT 1) GROUP BY city",
        "WITH top AS (SELECT customer_key FROM orders ORDER BY order_revenue DESC LIMIT 3) "
        "SELECT u.city, COUNT(*) AS n FROM users AS u JOIN top USING (customer_key) GROUP BY 1",
    ],
)
def test_rejects_identifier_sets_cut_below_the_minimum_group(sql: str) -> None:
    with pytest.raises(QueryRejected) as err:
        validate(sql, max_rows=MAX_ROWS)
    assert err.value.rule == "demographics_not_aggregated"


def test_identifier_sets_of_at_least_five_are_allowed() -> None:
    validate(
        "SELECT city, COUNT(*) AS n FROM users WHERE customer_key IN "
        "(SELECT customer_key FROM orders ORDER BY order_revenue DESC LIMIT 100) GROUP BY city",
        max_rows=MAX_ROWS,
    )
