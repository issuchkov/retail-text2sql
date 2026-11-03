"""Rule `demographics_not_aggregated` (SPEC §4.2, §4.4).

Demographic columns may reach the result only in aggregates. The query is qualified
against the logical schema, then two things are traced through aliases, CTEs, derived
tables, subqueries and set operations:

- the *origin* of each output column (which base columns it is computed from), and
- the *grain* of each SELECT: whether its rows are individual customers, orders or items.

A SELECT is individual-grained when it neither groups nor aggregates and reads from an
individual-grained source, or when it groups but its keys or outputs expose a row
identifier (`customer_key`, order or item ids — also through `ARRAY_AGG`, `MIN`, `MAX`;
counting or summing identifiers does not expose them). The rule fails when:

- the final result (or a subquery used as a value in it) outputs a demographic column at
  individual grain, or
- the result outputs demographics and a row identifier is used for anything but output,
  grouping, counting, joining on another identifier or matching an identifier subquery —
  `customer_key = 'abc'`, `STARTS_WITH(customer_key, ...)`, `COUNTIF(customer_key = ...)`
  all single out a customer, so a "group" built that way is not an aggregate.

Anything the tracer cannot resolve counts as both demographic and identifier when `users`
is in the query.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from retail_agent.query.schema import LOGICAL_TABLES

Origin = tuple[str, str]

DEMOGRAPHIC_COLUMNS: frozenset[Origin] = frozenset(
    ("users", c) for c in ("age", "gender", "city", "state", "country", "traffic_source")
)
IDENTIFIER_COLUMNS: frozenset[Origin] = frozenset(
    {
        ("users", "customer_key"),
        ("orders", "customer_key"),
        ("order_items", "customer_key"),
        ("orders", "order_id"),
        ("order_items", "order_id"),
        ("order_items", "id"),
        ("order_items", "inventory_item_id"),
    }
)
UNKNOWN: Origin = ("?", "?")
MIN_IDENTIFIER_SET = 5  # an identifier subquery cut below this by LIMIT singles people out

# Aggregates that reduce a group to a number and so do not expose individual values.
# ARRAY_AGG, STRING_AGG, MIN, MAX and ANY_VALUE return row values and are not listed.
_REDUCING: tuple[type[exp.Expression], ...] = (
    exp.Count,
    exp.CountIf,
    exp.ApproxDistinct,
    exp.Sum,
    exp.Avg,
    exp.Stddev,
    exp.StddevPop,
    exp.StddevSamp,
    exp.Variance,
    exp.VariancePop,
    exp.Corr,
    exp.CovarPop,
    exp.CovarSamp,
    exp.LogicalAnd,
    exp.LogicalOr,
)

_SCHEMA = {table: {c.name: c.type for c in cols} for table, cols in LOGICAL_TABLES.items()}

MESSAGE = (
    "Demographic columns (age, gender, city, state, country, traffic_source) can only be "
    "shown in aggregates: group by them and count, sum or average, with no customer_key, "
    "order or item ids in the result; ids may only be counted, grouped or joined on, not "
    "compared with values or passed to functions. For customer-level analysis leave "
    "demographics out of the output; filtering by them is fine."
)


def exposes_individual_demographics(tree: exp.Expression, tables: set[str]) -> bool:
    if "users" not in tables:
        return False  # demographics only exist in `users`
    try:
        qualified = qualify(
            tree.copy(),
            schema=_SCHEMA,
            dialect="bigquery",
            validate_qualify_columns=False,
            quote_identifiers=False,
        )
        root = build_scope(qualified)
    except SqlglotError:
        return True  # fail closed: cannot trace a query that reads demographics
    if root is None:
        return True
    tracer = _Tracer()
    return tracer.violates(root) or (
        tracer.outputs_demographics(root) and tracer.misuses_identifiers(root)
    )


class _Tracer:
    def violates(self, scope: Scope) -> bool:
        if isinstance(scope.expression, exp.SetOperation):
            return any(self.violates(branch) for branch in scope.set_operation_scopes)
        if not isinstance(scope.expression, exp.Select):
            return True
        if self.outputs_demographics(scope) and self.individual(scope):
            return True
        for p in scope.expression.expressions:
            for item in _walk(p, exposed_only=False):
                if isinstance(item, (exp.Column, exp.TableColumn)):
                    continue
                sub = _child_scope(scope, item)
                if sub is None or self.violates(sub):
                    return True
        return False

    def outputs_demographics(self, scope: Scope) -> bool:
        return any(
            self.origins(p, leaf, exposed_only=False) & (DEMOGRAPHIC_COLUMNS | {UNKNOWN})
            for leaf in _leaf_selects(scope)
            for p in leaf.expression.expressions
        )

    def misuses_identifiers(self, root: Scope) -> bool:
        """Is a row identifier used in any way that could single out one customer?

        Allowed: a plain identifier column that is output or grouped by (the grain rule
        judges those), counted, compared for equality with another identifier column (a
        join key), or matched against a subquery of identifiers. Anything else — a
        comparison with a value, a function, a condition inside an aggregate — is a misuse.
        """
        for scope in root.traverse():
            if self._small_identifier_set(scope):
                return True
            for column in scope.columns:
                origins = self.column_origins(column, scope, exposed_only=False)
                if origins & IDENTIFIER_COLUMNS and not self._allowed_identifier_use(column, scope):
                    return True
        return False

    def _small_identifier_set(self, scope: Scope) -> bool:
        """A step that outputs identifiers and is cut by LIMIT to fewer than 5 rows."""
        select = scope.expression
        limit = select.args.get("limit") if isinstance(select, exp.Select) else None
        if limit is None or scope.is_root:
            return False
        value = limit.expression
        if not (isinstance(value, exp.Literal) and value.is_int):
            return True  # a computed limit cannot be checked: fail closed
        if int(value.this) >= MIN_IDENTIFIER_SET:
            return False
        return any(
            self.origins(p, scope, exposed_only=False) & IDENTIFIER_COLUMNS
            for p in select.expressions
        )

    def _allowed_identifier_use(self, column: exp.Column, scope: Scope) -> bool:
        node: exp.Expression = column
        parent = node.parent
        while isinstance(parent, exp.Paren):
            node, parent = parent, parent.parent
        if isinstance(parent, exp.Select):
            return node in parent.expressions  # a bare projection
        if isinstance(parent, exp.Alias):
            return isinstance(parent.parent, exp.Select)  # a projection with an alias
        if isinstance(parent, (exp.Group, exp.Ordered, exp.Count, exp.ApproxDistinct)):
            return True
        if isinstance(parent, exp.Distinct):
            return isinstance(parent.parent, (exp.Count, exp.ApproxDistinct))
        if isinstance(parent, exp.Window):
            return node in (parent.args.get("partition_by") or [])
        if isinstance(parent, exp.EQ):
            other = parent.expression if parent.this is node else parent.this
            while isinstance(other, exp.Paren):
                other = other.this
            return isinstance(other, exp.Column) and bool(
                self.column_origins(other, scope, exposed_only=False) & IDENTIFIER_COLUMNS
            )
        if isinstance(parent, exp.In) and parent.this is node and parent.args.get("query"):
            sub = _child_scope(scope, parent.args["query"])
            return sub is not None and all(
                self.origins(p, leaf, exposed_only=False) & IDENTIFIER_COLUMNS
                for leaf in _leaf_selects(sub)
                for p in leaf.expression.expressions
            )
        return False

    def individual(self, scope: Scope) -> bool:
        select = scope.expression
        if isinstance(select, exp.SetOperation):
            return any(self.individual(branch) for branch in scope.set_operation_scopes)
        if not isinstance(select, exp.Select):
            return False  # UNNEST and other table functions: grain comes from other sources
        group = select.args.get("group")
        distinct = select.args.get("distinct")
        projections = list(select.expressions)
        if group or distinct or any(_has_aggregate(p) for p in projections):
            keys = list(group.expressions) if group else []
            exposed = IDENTIFIER_COLUMNS | {UNKNOWN}
            return any(
                self.origins(e, scope, exposed_only=True) & exposed for e in keys + projections
            )
        sources = [source for _, source in scope.selected_sources.values()]
        return any(isinstance(s, exp.Table) or self.individual(s) for s in sources)

    def origins(self, node: exp.Expression, scope: Scope, *, exposed_only: bool) -> set[Origin]:
        found: set[Origin] = set()
        for item in _walk(node, exposed_only):
            if isinstance(item, exp.Column):
                found |= self.column_origins(item, scope, exposed_only=exposed_only)
            elif isinstance(item, exp.TableColumn):  # a whole row used as a value
                found |= self.whole_row_origins(item.name.lower(), scope, exposed_only)
            else:  # a subquery used as a value
                sub = _child_scope(scope, item)
                if sub is None:
                    found.add(UNKNOWN)
                else:
                    for leaf in _leaf_selects(sub):
                        for p in leaf.expression.expressions:
                            found |= self.origins(p, leaf, exposed_only=exposed_only)
        return found

    def column_origins(
        self, column: exp.Column, scope: Scope, *, exposed_only: bool
    ) -> set[Origin]:
        name = column.name.lower()
        if not column.table:
            select = scope.expression
            if isinstance(select, exp.Select):
                for p in select.expressions:
                    if p.alias_or_name.lower() == name and isinstance(p, exp.Alias):
                        return self.origins(p.this, scope, exposed_only=exposed_only)
            if not self._is_source_name(name, scope):
                unnested = self._unnest_origins(scope, exposed_only)
                if unnested is not None:
                    return unnested
            return self.whole_row_origins(name, scope, exposed_only)
        current: Scope | None = scope
        while current is not None:  # correlated references resolve in an outer scope
            if column.table in current.selected_sources:
                source = current.selected_sources[column.table][1]
                if isinstance(source, exp.Table):
                    return {(source.name.lower(), name)}
                if isinstance(source.expression, exp.Unnest):
                    return self._columns_in(source.expression, current, exposed_only)
                return self.projection_origins(source, name, exposed_only)
            current = current.parent
        return {UNKNOWN}

    def _is_source_name(self, name: str, scope: Scope) -> bool:
        current: Scope | None = scope
        while current is not None:
            if name in current.selected_sources:
                return True
            current = current.parent
        return False

    def _unnest_origins(self, scope: Scope, exposed_only: bool) -> set[Origin] | None:
        """Origins of a value produced by UNNEST in this scope: those of the columns inside
        it — none for an array of literals (`UNNEST([DATE '2026-08-31', ...])`)."""
        for _, source in scope.selected_sources.values():
            if isinstance(source, Scope) and isinstance(source.expression, exp.Unnest):
                return self._columns_in(source.expression, scope, exposed_only)
        return None

    def _columns_in(self, node: exp.Expression, scope: Scope, exposed_only: bool) -> set[Origin]:
        found: set[Origin] = set()
        for column in node.find_all(exp.Column):
            found |= self.column_origins(column, scope, exposed_only=exposed_only)
        return found

    def whole_row_origins(self, name: str, scope: Scope, exposed_only: bool) -> set[Origin]:
        current: Scope | None = scope
        while current is not None:
            if name in current.selected_sources:
                return self.row_origins(current.selected_sources[name][1], exposed_only)
            current = current.parent
        return {UNKNOWN}

    def projection_origins(self, scope: Scope, name: str, exposed_only: bool) -> set[Origin]:
        leaves = _leaf_selects(scope)
        if not leaves:
            return {UNKNOWN}
        names = [p.alias_or_name.lower() for p in leaves[0].expression.expressions]
        if name not in names:
            return {UNKNOWN}
        index = names.index(name)
        found: set[Origin] = set()
        for leaf in leaves:  # set operations project by position
            projections = leaf.expression.expressions
            if index >= len(projections):
                return {UNKNOWN}
            found |= self.origins(projections[index], leaf, exposed_only=exposed_only)
        return found

    def row_origins(self, source: exp.Table | Scope, exposed_only: bool) -> set[Origin]:
        if isinstance(source, exp.Table):
            table = source.name.lower()
            return {(table, c.name) for c in LOGICAL_TABLES.get(table, ())} or {UNKNOWN}
        found: set[Origin] = set()
        for leaf in _leaf_selects(source):
            for p in leaf.expression.expressions:
                found |= self.origins(p, leaf, exposed_only=exposed_only)
        return found or {UNKNOWN}


def _walk(node: exp.Expression, exposed_only: bool) -> Iterator[exp.Expression]:
    """Columns and value subqueries in `node`, without descending into subqueries."""
    if isinstance(node, (exp.Column, exp.TableColumn)):
        yield node
        return
    if isinstance(node, (exp.Subquery, exp.Select, exp.SetOperation)):
        yield node
        return
    if exposed_only and isinstance(node, _REDUCING) and not isinstance(node.parent, exp.Window):
        return
    for child in node.iter_expressions():
        yield from _walk(child, exposed_only)


def _has_aggregate(node: exp.Expression) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (exp.Subquery, exp.Select)):
            continue
        if isinstance(current, exp.Window):
            stack.extend(current.args.get("partition_by") or [])
            continue
        if isinstance(current, exp.AggFunc):
            return True
        stack.extend(current.iter_expressions())
    return False


def _child_scope(scope: Scope, node: exp.Expression) -> Scope | None:
    target = node.this if isinstance(node, exp.Subquery) else node
    for child in scope.subquery_scopes:
        if child.expression is target or child.expression is node:
            return child
    return None


def _leaf_selects(scope: Scope) -> list[Scope]:
    if isinstance(scope.expression, exp.SetOperation):
        leaves: list[Scope] = []
        for branch in scope.set_operation_scopes:
            leaves.extend(_leaf_selects(branch))
        return leaves
    return [scope] if isinstance(scope.expression, exp.Select) else []
