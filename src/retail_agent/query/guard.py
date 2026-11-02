"""Deterministic SQL guard and scope binding (SPEC §4.3, §4.4).

`validate` checks model-written SQL against the logical schema and rejects anything that is
not a single read-only query over the four logical tables. `bind` prepends the scoped,
PII-free CTEs for the current user's entitlement. Rejections carry a rule id (for metrics)
and a message the model can act on when it corrects the query.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from retail_agent.config import Entitlement
from retail_agent.query.demographics import MESSAGE as DEMOGRAPHICS_MESSAGE
from retail_agent.query.demographics import exposes_individual_demographics
from retail_agent.query.schema import LOGICAL_TABLES, PII_COLUMNS, RAW_CUSTOMER_ID_COLUMNS
from retail_agent.query.scope import QueryParam, scoped_ctes

DIALECT = "bigquery"

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.SessionUser,
    exp.CurrentUser,
    exp.Predict,
)
_FORBIDDEN_FUNCTION_PREFIXES = ("ML.", "AI.", "VECTOR_SEARCH", "EXTERNAL_QUERY", "EXTERNAL_OBJECT")


class QueryRejected(Exception):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"{rule}: {message}")
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class ValidatedQuery:
    tree: exp.Query
    tables: set[str]
    limit: int


@dataclass(frozen=True)
class PreparedQuery:
    sql: str
    params: list[QueryParam] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)


def validate(sql: str, *, max_rows: int) -> ValidatedQuery:
    if not sql or not sql.strip():
        raise QueryRejected("syntax", "The query is empty.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        raise QueryRejected("syntax", f"SQL syntax error: {_first_line(e)}") from None
    if len(statements) != 1:
        raise QueryRejected(
            "multiple_statements", "Send exactly one SELECT statement, without semicolons."
        )
    tree = statements[0]
    if not isinstance(tree, exp.Query) or isinstance(tree, exp.Subquery):
        raise QueryRejected("not_select", "Only read-only SELECT queries are allowed.")

    _check_functions(tree)
    _check_parameters(tree)
    tables = _check_tables(tree)
    _check_columns(tree)
    if exposes_individual_demographics(tree, tables):
        raise QueryRejected("demographics_not_aggregated", DEMOGRAPHICS_MESSAGE)
    limit = _apply_limit(tree, max_rows)
    return ValidatedQuery(tree=tree, tables=tables, limit=limit)


def bind(query: ValidatedQuery, scope: Entitlement, *, dataset: str, salt: str) -> PreparedQuery:
    """Prepend the scoped CTEs the query needs; returns SQL plus BigQuery parameters."""
    ctes, params = scoped_ctes(query.tables, scope, dataset=dataset, salt=salt)
    tree = query.tree.copy()
    own = tree.args.get("with_")
    scoped = [
        exp.CTE(this=sqlglot.parse_one(body, read=DIALECT), alias=exp.TableAlias(this=name))
        for name, body in ctes
    ]
    tree.set("with_", exp.With(expressions=scoped + (own.expressions if own else [])))
    return PreparedQuery(sql=tree.sql(dialect=DIALECT), params=params, tables=set(query.tables))


def _check_functions(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise QueryRejected(
                "forbidden_function", f"{node.sql(DIALECT)} is not allowed in analysis queries."
            )
        if isinstance(node, exp.Anonymous) and str(node.name).upper().startswith(
            _FORBIDDEN_FUNCTION_PREFIXES
        ):
            raise QueryRejected(
                "forbidden_function", f"Function {node.name} is not allowed in analysis queries."
            )


def _check_parameters(tree: exp.Expression) -> None:
    if tree.find(exp.Parameter, exp.Placeholder):
        raise QueryRejected(
            "parameter", "Query parameters are not supported; write literal values instead."
        )


def _check_tables(tree: exp.Expression) -> set[str]:
    own_ctes = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    shadowed = own_ctes & LOGICAL_TABLES.keys()
    if shadowed:
        raise QueryRejected(
            "shadowed_table",
            f"Do not name a CTE {', '.join(sorted(shadowed))}; that name is a data table.",
        )
    used: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not table.name and isinstance(table.this, exp.Func):
            continue  # table-valued function, already checked by _check_functions
        if table.args.get("db") or table.args.get("catalog") or "." in table.name:
            raise QueryRejected(
                "qualified_table",
                f"Use the table names {_tables_list()} without dataset or project prefixes.",
            )
        name = table.name.lower()
        if name in own_ctes:
            continue
        if name not in LOGICAL_TABLES:
            raise QueryRejected(
                "unknown_table", f"Unknown table {table.name!r}. Available: {_tables_list()}."
            )
        table.set("this", exp.to_identifier(name))  # bind to the scoped CTE, whatever the case
        used.add(name)
    return used


def _check_columns(tree: exp.Expression) -> None:
    for column in tree.find_all(exp.Column):
        name = column.name.lower()
        if name in PII_COLUMNS:
            raise QueryRejected(
                "pii_column",
                f"{column.name} is personal data and is not available. Analyse customers by "
                "age, gender, city, state, country or traffic_source, or by customer_key.",
            )
        if name in RAW_CUSTOMER_ID_COLUMNS:
            raise QueryRejected(
                "raw_customer_id",
                "Customer ids are pseudonymised: use customer_key instead of user_id.",
            )


def _apply_limit(tree: exp.Query, max_rows: int) -> int:
    current = tree.args.get("limit")
    value = max_rows
    if current is not None:
        given = current.expression
        if isinstance(given, exp.Literal) and given.is_int:
            value = min(int(given.this), max_rows)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(value)))
    return value


def _tables_list() -> str:
    return ", ".join(LOGICAL_TABLES)


def _first_line(error: ParseError) -> str:
    return str(error).splitlines()[0][:300]
