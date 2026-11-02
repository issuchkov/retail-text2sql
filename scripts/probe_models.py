"""Stage 1 probe: can a model call our query tool with valid SQL, and how fast? (PLAN §1)

For each model and question: one model call with the logical schema and a `run_query`
tool. Reports latency, whether the model called the tool, whether its SQL passes the
guard, and whether the bound query passes a BigQuery dry run (free). Usage:

    uv run python scripts/probe_models.py gemini-3.5-flash-lite gemini-3.1-flash-lite
"""

from __future__ import annotations

import sys
import time

from google import genai
from google.genai import types

from retail_agent.config import Entitlement, Settings, load_env
from retail_agent.query.executor import QueryFailed, make_executor
from retail_agent.query.guard import QueryRejected, bind, validate
from retail_agent.query.schema import describe_schema

QUESTIONS = [
    "What were the top 5 product categories by revenue in the last full calendar quarter?",
    "How many customers do we have in each country?",
    "Show the monthly revenue trend for 2025.",
    "Which traffic source brings customers with the highest average order value?",
]

SYSTEM = f"""You are a data analyst for a retail company. Answer questions by calling
run_query with one BigQuery Standard SQL SELECT over these tables (use the names exactly,
without dataset prefixes):

{describe_schema()}

Revenue = sum of sale_price of items whose status is not Cancelled or Returned
(orders.order_revenue is already net). Quarters are calendar quarters in UTC. Today is
{time.strftime("%Y-%m-%d")}. Demographic columns may only appear in aggregates."""

RUN_QUERY = types.FunctionDeclaration(
    name="run_query",
    description="Run one read-only BigQuery SELECT over the tables above.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={"sql": types.Schema(type=types.Type.STRING, description="The query")},
        required=["sql"],
    ),
)


def probe(client: genai.Client, executor, settings: Settings, model: str, question: str):
    started = time.monotonic()
    try:
        response = client.models.generate_content(
            model=model,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                tools=[types.Tool(function_declarations=[RUN_QUERY])],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except Exception as e:
        return time.monotonic() - started, f"model error {type(e).__name__}: {str(e)[:120]}", ""
    latency = time.monotonic() - started
    calls = response.function_calls or []
    if not calls:
        return latency, "no tool call", (response.text or "")[:120]
    sql = calls[0].args.get("sql", "")
    try:
        prepared = bind(
            validate(sql, max_rows=settings.max_result_rows),
            Entitlement(all=True),
            dataset=settings.dataset,
            salt=settings.pseudonym_salt or "probe",
        )
    except QueryRejected as e:
        return latency, f"guard: {e.rule}", sql
    try:
        mb = executor.dry_run(prepared) / 1e6
    except QueryFailed as e:
        return latency, f"dry run: {e.message[:100]}", sql
    return latency, f"ok ({mb:.0f} MB)", sql


def main(models: list[str]) -> None:
    load_env()
    settings = Settings()
    client = genai.Client()
    executor = make_executor(settings)
    for model in models:
        print(f"\n## {model}")
        for question in QUESTIONS:
            latency, verdict, sql = probe(client, executor, settings, model, question)
            print(f"{latency:6.1f}s  {verdict:28}  {question}")
            if sql:
                print(f"         {' '.join(sql.split())[:220]}")


if __name__ == "__main__":
    main(sys.argv[1:] or [Settings().model, Settings().fallback_model])
