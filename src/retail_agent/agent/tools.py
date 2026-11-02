"""Tools the model can call (SPEC §3, §4, §5).

Identity and scope always come from the session (`tool_context.session.user_id`), never
from tool arguments, so the model cannot act as another user or widen its scope. Every
query goes through the guard, the scope binding and the executor; results are redacted
and size-capped before they are returned to the model as data.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from google.adk.tools import FunctionTool, ToolContext
from pydantic import ValidationError

from retail_agent.agent import budget
from retail_agent.agent.redaction import redact, redact_value
from retail_agent.agent.services import Services
from retail_agent.config import UserProfile
from retail_agent.query.executor import QueryFailed
from retail_agent.query.guard import QueryRejected, bind, validate
from retail_agent.query.schema import LOGICAL_TABLES, describe_schema
from retail_agent.reports.deletion import execute_deletion, plan_deletion
from retail_agent.reports.store import Selector

MODEL_MAX_ROWS = 200
MODEL_MAX_CHARS = 20_000
PENDING_DELETE = "pending_delete"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

EMPTY_HINT = (
    "The query returned no rows. Check filter values and their spelling (call "
    "list_column_values to see the real values) and the date range covered by the data."
)


def build_tools(services: Services) -> list[FunctionTool]:
    def current_user(tool_context: ToolContext) -> UserProfile:
        return services.user(tool_context.session.user_id)

    def execute(sql: str, user: UserProfile, tool_context: ToolContext) -> dict[str, Any]:
        settings = services.settings
        spent = budget.counters(tool_context.state, tool_context.invocation_id)
        if spent["failed"] >= settings.max_failed_queries:
            return {
                "status": "budget_exhausted",
                "message": (
                    f"{spent['failed']} query attempts failed in this turn. Stop querying and "
                    "tell the user what you tried and what went wrong."
                ),
            }
        if spent["queries"] >= settings.max_queries_per_turn:
            return {
                "status": "budget_exhausted",
                "message": (
                    f"The limit of {settings.max_queries_per_turn} queries per question is "
                    "reached. Answer with the results you already have."
                ),
            }
        response = _execute(sql, user, spent)
        budget.save(tool_context.state, spent)
        return response

    def _execute(sql: str, user: UserProfile, spent: dict[str, Any]) -> dict[str, Any]:
        settings = services.settings
        try:
            prepared = bind(
                validate(sql, max_rows=settings.max_result_rows),
                user.scope,
                dataset=settings.dataset,
                salt=settings.pseudonym_salt or "",
            )
        except QueryRejected as e:
            spent["failed"] += 1
            return {"status": "rejected", "rule": e.rule, "message": e.message}
        spent["queries"] += 1
        try:
            result = services.executor.run(prepared, max_rows=settings.max_result_rows)
        except QueryFailed as e:
            spent["failed"] += 1
            return {
                "status": "error",
                "kind": e.kind,
                "message": e.message,
                "can_fix_by_rewriting_sql": e.model_can_fix,
            }
        rows, kinds = redact_value(_fit(result.rows))
        response: dict[str, Any] = {
            "status": "ok",
            "columns": result.columns,
            "rows": rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "bytes_processed": result.bytes_processed,
        }
        if len(rows) < result.row_count:
            response["rows_shown"] = len(rows)
        if kinds:
            response["redacted"] = kinds
        if not result.rows:
            response["hint"] = EMPTY_HINT
        return response

    def run_query(sql: str, tool_context: ToolContext, purpose: str = "") -> dict[str, Any]:
        """Run one read-only BigQuery Standard SQL SELECT over the tables orders,
        order_items, products and users (names exactly as given, no dataset prefix).

        Args:
            sql: A single SELECT statement. Demographic columns only in aggregates.
            purpose: One short sentence: what this query answers.

        Returns:
            status "ok" with columns and rows; "rejected" with the guard rule and how to fix
            the query; or "error" with the BigQuery error and whether rewriting can fix it.
        """
        return execute(sql, current_user(tool_context), tool_context)

    def list_column_values(
        table: str, column: str, tool_context: ToolContext, limit: int = 50
    ) -> dict[str, Any]:
        """List the most frequent values of one column, to get filter spellings right.

        Args:
            table: One of orders, order_items, products, users.
            column: A column of that table.
            limit: How many values to return (at most 200).
        """
        if table not in LOGICAL_TABLES or not _IDENTIFIER.match(column):
            return {
                "status": "rejected",
                "rule": "unknown_table",
                "message": f"Use one of {', '.join(LOGICAL_TABLES)} and a plain column name.",
            }
        top = max(1, min(int(limit), 200))
        sql = (
            f"SELECT {column} AS value, COUNT(*) AS n FROM {table} "
            f"GROUP BY value ORDER BY n DESC LIMIT {top}"
        )
        return execute(sql, current_user(tool_context), tool_context)

    def describe_data(tool_context: ToolContext) -> dict[str, Any]:
        """Describe the available tables and columns, the user's brand scope and the date
        range the data covers. Returns no customer data."""
        user = current_user(tool_context)
        coverage = services.coverage_cache.get(user.id)
        if coverage is None:
            result = execute(
                "SELECT MIN(created_at) AS first_order, MAX(created_at) AS last_order, "
                "COUNT(*) AS orders FROM orders",
                user,
                tool_context,
            )
            coverage = result["rows"][0] if result.get("rows") else {}
            if coverage:
                services.coverage_cache[user.id] = coverage
        return {
            "tables": describe_schema(),
            "your_scope": user.scope.describe(),
            "coverage": coverage,
        }

    def save_report(
        title: str, body: str, tool_context: ToolContext, entities: list[str] | None = None
    ) -> dict[str, Any]:
        """Save a report to the user's library. Only call this when the user asks to save.

        Args:
            title: Short descriptive title.
            body: The full report in Markdown.
            entities: Brands, categories, states and other entities the report is about.
        """
        user = current_user(tool_context)
        clean_title, kinds_title = redact(title)
        clean_body, kinds_body = redact(body)
        clean_entities, kinds_entities = redact_value(list(entities or []))
        report = services.store.save(
            user.id,
            tool_context.session.id,
            title=clean_title,
            body=clean_body,
            entities=clean_entities,
        )
        response: dict[str, Any] = {"status": "saved", "report_id": report.id}
        kinds = list(dict.fromkeys(kinds_title + kinds_body + kinds_entities))
        if kinds:
            response["redacted"] = kinds
        return response

    def list_reports(tool_context: ToolContext, mentions: str | None = None) -> dict[str, Any]:
        """List the user's own saved reports, optionally only those mentioning a text.

        Args:
            mentions: Optional text (a brand, category, client name...) to filter by.
        """
        user = current_user(tool_context)
        if mentions and mentions.strip():
            reports = services.store.resolve(
                user.id, Selector(mentions=mentions), tool_context.session.id
            )
        else:
            reports = services.store.list(user.id)
        return {
            "reports": [
                {
                    "id": r.id,
                    "title": r.title,
                    "created": r.created_at.date().isoformat(),
                    "entities": r.entities,
                }
                for r in reports
            ]
        }

    def delete_reports(
        tool_context: ToolContext,
        ids: list[int] | None = None,
        mentions: str | None = None,
        this_conversation: bool = False,
        older_than: str | None = None,
    ) -> dict[str, Any]:
        """Permanently delete some of the user's own saved reports. The system shows the
        user the exact list and asks them to confirm; nothing is deleted until they do.
        Criteria combine with AND; at least one is required.

        Args:
            ids: Report ids.
            mentions: Delete reports whose title, text or entities mention this text.
            this_conversation: Delete reports saved in the current conversation.
            older_than: ISO date (YYYY-MM-DD); delete reports created before it.
        """
        user = current_user(tool_context)
        trace_id = tool_context.invocation_id
        confirmation = tool_context.tool_confirmation
        if confirmation is not None:
            pending = tool_context.state.get(PENDING_DELETE) or {}
            tool_context.state[PENDING_DELETE] = None
            typed = ""
            if confirmation.confirmed and isinstance(confirmation.payload, dict):
                typed = str(confirmation.payload.get("typed", ""))
            outcome = execute_deletion(
                services.store,
                user.id,
                list(pending.get("ids", [])),
                typed=typed,
                trace_id=trace_id,
            )
            if outcome.status == "deleted":
                return {"status": "deleted", "deleted_ids": outcome.ids}
            return {
                "status": "cancelled",
                "message": "The user did not confirm. Nothing was deleted.",
            }

        try:
            selector = Selector(
                ids=ids,
                mentions=mentions,
                this_conversation=this_conversation,
                older_than=dt.date.fromisoformat(older_than) if older_than else None,
            )
        except (ValidationError, ValueError) as e:
            return {"status": "invalid_selector", "message": _first_error(e)}

        plan = plan_deletion(
            services.store, user.id, selector, tool_context.session.id, trace_id=trace_id
        )
        if plan.status == "nothing_matched":
            return {"status": "nothing_matched", "message": "No saved reports match."}
        if plan.status == "too_many":
            return {
                "status": "too_many",
                "matched": plan.matched,
                "message": "Too many reports match; ask the user to narrow the request.",
            }
        tool_context.state[PENDING_DELETE] = {"ids": plan.ids}
        tool_context.request_confirmation(
            hint=f"Permanently delete {len(plan.ids)} report(s)? Type '{plan.phrase}' to confirm.",
            payload={"items": plan.items, "phrase": plan.phrase},
        )
        return {
            "status": "awaiting_confirmation",
            "count": len(plan.ids),
            "message": (
                "Nothing has been deleted yet. The user is being shown the exact list and "
                "asked to confirm. Do not say the reports were deleted."
            ),
        }

    return [
        FunctionTool(run_query),
        FunctionTool(list_column_values),
        FunctionTool(describe_data),
        FunctionTool(save_report),
        FunctionTool(list_reports),
        FunctionTool(delete_reports),
    ]


def _fit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cap rows by count and serialized size so one result cannot flood the context."""
    shown = rows[:MODEL_MAX_ROWS]
    while shown and len(json.dumps(shown, default=str)) > MODEL_MAX_CHARS:
        shown = shown[: max(1, len(shown) // 2)] if len(shown) > 1 else []
    return shown


def _first_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return error.errors()[0]["msg"]
    return str(error)
