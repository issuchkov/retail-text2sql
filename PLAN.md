# PLAN — Retail Data Analysis Chat Agent

Resume point after context loss. Read `SPEC.md` first; this file says what is done, what
is next, and what counts as evidence.

Process: spec → plan → tests first for non-LLM logic → code. Tests for the query layer,
report store, guards and CLI plumbing run offline with fakes (`make test`); fake-model
results are not evidence of answer quality, only `make eval` against Gemini + BigQuery is.
No commits until the author says so.

Time budget: 6–12 h total. Estimates below add up to ~10 h; the cut order is at the end.

## Status

| Stage | Status |
|---|---|
| 0 Scaffold | done |
| 1 Model and quota check | todo |
| 2 Query layer (scoping, guard, executor) | done |
| 3 Report store and confirmation | done (tool wiring in stage 4) |
| 4 Agent assembly | todo |
| 5 CLI | todo |
| 6 Resilience | todo |
| 7 Observability | todo |
| 8 Golden bucket lite + preferences | todo |
| 9 Evals | todo |
| 10 Docs (HLD, technical explanation, README) | todo |
| 11 Fresh-clone check | todo |

## Environment (done before this plan)

- Personal gcloud config isolated in `~/.config/gcloud-personal` (`CLOUDSDK_CONFIG`); the
  work config is not touched.
- GCP project `retail-text2sql`, BigQuery API enabled, ADC quota project set. Verified:
  dry runs and real queries from this project against `bigquery-public-data.thelook_ecommerce`,
  schemas of all four tables.
- Data facts used in tests and evals: orders 124,916; order_items 180,946; products 29,120;
  users 100,000; orders from 2019-01-20 to 2026-10-28 (synthetic, refreshed daily — counts
  drift, so evals compare against reference SQL run at eval time, never hardcoded numbers);
  26 categories, 2 departments, 2,756 brands, 10 distribution centers.
- Google ADK 2.13.0 APIs checked in the installed package: callable `instruction`,
  `FunctionTool(require_confirmation=…)` / `ToolContext.request_confirmation`,
  before/after model and tool callbacks, `BasePlugin`, `DatabaseSessionService`,
  `google.adk.tools.bigquery` (`BigQueryToolConfig.write_mode`, `maximum_bytes_billed`).

## Stages

### 0 Scaffold (0.5 h)
- `pyproject.toml` (uv, Python 3.12): `google-adk`, `google-cloud-bigquery`, `db-dtypes`,
  `sqlglot`, `pyyaml`, `pydantic-settings`, `rich`; dev: `pytest`, `pytest-asyncio`, `ruff`.
- `.gitignore` (`.env`, `data/*.sqlite`, `logs/`, `.venv/`), `.env.example` with names and
  placeholders only: `GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_APPLICATION_CREDENTIALS`, model names, budgets.
- `Makefile`: `setup`, `test`, `lint`, `eval`, `run`.
- Layout: `src/retail_agent/{config,query,reports,agent,cli,observability,golden}`,
  `config/users.yaml`, `config/persona.md`, `golden/`, `evals/`, `tests/`, `docs/`.
- Evidence: `make test` (empty suite) and `make lint` pass.

### 1 Model and quota check (0.5 h)
- The author puts the free Gemini key into `.env` through a hidden prompt (never in chat).
- Script lists the models available to the key; a short tool-calling + SQL-writing probe on
  2–3 Flash candidates: latency, tool-call correctness, free-tier RPM/RPD.
- Decide primary and fallback models; record in `docs/adr/0003-model.md`.
- Evidence: probe output summarised in the ADR. Risk: free-tier daily limits during evals
  → evals run small and once per change; the paid key later only changes `.env`.

### 2 Query layer (2 h, TDD) — SPEC §4.3, §4.4, §6
RED tests first, then code:
- `scope.py`: builds the scoped, PII-free CTEs for an entitlement; tests assert that the
  generated SQL filters products by the entitlement, drops PII columns, replaces user ids
  by `customer_key`, and recomputes order totals over entitled items.
- `guard.py` (sqlglot, BigQuery dialect): table cases for AC-4 — DML/DDL/scripting/`EXPORT`,
  multiple statements, base or fully-qualified tables, wildcard tables, PII columns
  (including via `SELECT *` on `users` and aliases), CTE names shadowing logical names,
  missing `LIMIT` → added; valid analytical queries pass unchanged apart from binding.
- `executor.py`: dry run → byte cap → run with `maximum_bytes_billed`, timeout, row cap;
  error classification (syntax/semantic, empty, resource/permission, transient) with a
  fake BigQuery client. (Small-group suppression moved to design: SPEC §4.2.)
- Live smoke tests (marked `live`, skipped offline): a scoped query returns only entitled
  categories; dry-run bytes stay under the cap for typical queries.
- Evidence: unit suite green; live smoke output recorded here.
- Done 2026-10-29: 67 offline tests (guard 41 cases, scope 11, executor 10, config 5);
  `make test-live` 6/6 on BigQuery — Bob sees only `Outerwear & Coats` + `Jeans`, 0 items
  outside scope, `SUM(order_revenue)` = `SUM(sale_price)` within scope, 16-hex customer
  keys join orders↔users, scoped users ⊂ all users, monthly revenue × country join
  dry-runs at 11 MB (cap 1 GB).

### 3 Report store and confirmation (1 h, TDD) — SPEC §5
- `reports/store.py` (SQLite): save, list own, resolve selector (`ids`, `mentions`,
  `this_conversation`, `older_than`) to owned, non-deleted reports; soft delete by exact
  ids; undo; bulk limit 20.
- Tests: other users' reports never matched; delete uses previewed ids even if new matching
  reports appear between preview and confirm; nothing matched → no confirmation.
- Spike (first, 20 min): ADK tool confirmation round trip with the Runner in a script —
  how the CLI receives the confirmation request event and returns the `ToolConfirmation`.
  If it does not fit, fall back to a two-step tool (`prepare_delete` → token checked by
  `confirm_delete` that only the CLI can supply) and record why.
- Evidence: unit suite green; spike result noted in the decisions log.
- Done 2026-10-29: store + two-phase deletion, 19 tests (owner scoping, selectors, bulk
  limit, exact phrase, previewed ids only, already-deleted reports, file persistence).

### 4 Agent assembly (1.5 h) — SPEC §3, §4.5
- ADK `LlmAgent` with tools: `describe_data` (logical schema + coverage, no rows),
  `run_query`, `list_column_values`, `save_report`, `list_reports`, `delete_reports`,
  `find_similar_analyses` (stage 8), `remember_preference` (stage 8).
- Callable `instruction`: persona file + current user's scope and preferences + rules
  (data in delimited blocks is data; refuse out-of-scope topics; state assumptions;
  never output PII). Re-read every turn → persona changes without restart (R8 lite).
- `after_model` / output callback: PII pattern redaction of the final answer.
- Tools take identity from the session state, never from model arguments.
- Tests with a fake model: identity cannot be overridden by tool args; redaction applied.
- Evidence: a manual session answering one question of each SPEC §3.1 type.

### 5 CLI (0.5 h) — SPEC §3.3, §5.3
- `retail-agent chat --user <id>`; `rich` rendering of markdown and tables.
- Commands: `/sql`, `/trace`, `/stats`, `/reports`, `/undo`, `/new`, `/quit`.
- Confirmation prompt: shows the preview and requires `delete N`.
- Sessions in SQLite (`DatabaseSessionService`) → resume after restart.
- Every turn wrapped: errors → short message with `trace_id`, loop continues.
- Tests: confirmation parsing (exact `delete N` only), error wrapping.

### 6 Resilience (1 h) — SPEC §6, AC-6, AC-7
- Model calls: retry with backoff and jitter on 429/5xx/timeout, then fallback model;
  circuit breaker per model; per-turn budgets (model calls, queries).
- SQL self-correction: classified errors returned to the model with hints; max 3 attempts
  per question, enforced in code (tool returns "budget exhausted").
- Fault-injection tests: fake model raising 429 then succeeding; permanent failure →
  fallback → clear message; BigQuery transient vs permanent errors; budget exhaustion.

### 7 Observability (0.5 h) — SPEC §7, AC-8
- ADK `BasePlugin` writing JSON-lines spans to `logs/traces.jsonl`: turn, model call
  (tokens, latency, retries), tool call (SQL, guard decision, bytes, rows), confirmations,
  redactions. Questions and answers logged redacted.
- `/trace` (last turn timeline), `/stats` (session metrics from SPEC §7).
- Tests: a fake turn produces the expected span sequence; no PII in trace lines.
- Note in docs: ADK OpenTelemetry → Cloud Trace for production.

### 8 Golden bucket lite + preferences (1 h) — SPEC §8 R1, R4 (cut first)
- `golden/trios/*.yaml`: 20–30 Question → SQL (logical table names) → report summary,
  written by us; every SQL checked by the guard and run once against BigQuery.
- Retrieval: Gemini embeddings precomputed into `golden/index.json`; cosine top-3 as
  examples in `find_similar_analyses`. If embedding quota is a problem, a local TF-IDF
  index with the same interface.
- `remember_preference`: per-user preferences in SQLite, injected into the instruction.

### 9 Evals (1 h) — SPEC §11
- `evals/scenarios.yaml`: ~15 questions covering SPEC §3.1 types; each has a reference SQL
  run at eval time; checks: numbers in the answer match the reference, assumptions stated.
- `evals/redteam.yaml`: ~15 prompts — PII requests, re-identification, out-of-scope
  categories, prompt injection, write/DDL requests, other users' reports, delete without
  confirmation. Checks are deterministic (trace + answer scan), not model-judged.
- `make eval` writes a summary table; results copied into `docs/eval-results.md`.
- Evidence: real runs only; failing cases recorded, not edited away.

### 10 Docs (1.5 h)
- `docs/HLD.md`: Mermaid architecture diagram (CLI/UI → agent runtime → guarded tools →
  BigQuery; golden bucket, report library, persona config, memory, tracing/metrics,
  eval pipeline) and the component descriptions.
- `docs/technical-explanation.md`: each requirement R1–R8 — design, technology choice and
  reasoning, what the prototype implements vs production; cost and scaling notes.
- `docs/adr/`: 0001 agent framework (ADK), 0002 scoping and PII in the query layer, 0003
  model choice.
- `README.md`: what it is, setup on a clean machine (uv, gcloud ADC with any project,
  Gemini key), example session, commands, tests, limitations.

### 11 Fresh-clone check (0.5 h) — AC-9
- Clone into a temporary directory, follow README only, run `make test` and one chat
  session. Fix the README, not the machine.

## Cut order if over time
1. Stage 8 retrieval falls back to TF-IDF; preferences dropped (design only).
2. Eval scenarios reduced to 8 + red-team 10.
3. `/undo` and session resume dropped (documented).
Never cut: SQL guard + scoping, PII masking, delete confirmation, resilience, tracing.

## Risks
- Gemini free tier: daily request limits and latency swings → small eval runs, retries,
  fallback model, budgets; switch to the paid key via `.env` only.
- Model writing SQL against logical names it has not seen as real tables → schema
  description + golden examples; guard errors are informative for self-correction.
- Scoped CTEs scan base tables on every query (tens of MB) → fine for this dataset and the
  BigQuery free tier; production would use authorized views or row-level security (HLD).
- ADK confirmation API is new → spike first in stage 3, fallback design ready.

## Decisions log
- 2026-10-29: Google ADK chosen by the author (Gemini-native, tool confirmation, plugins);
  own guarded executor instead of ADK `execute_sql` for row data (SPEC §9).
- 2026-10-29: Free Gemini key for development; paid key later via `.env`.
- 2026-10-29: Docs and code in English; no commits until the author asks.
- 2026-10-29: Small-group (k) suppression moved from the prototype to the design — a generic
  post-hoc check cannot tell which result rows describe customers; production uses BigQuery
  aggregation threshold rules. Layout: `src/retail_agent`, top-level `tests/`, `evals/`.
- 2026-10-29: Confirmation spike (fake LLM, ADK 2.13.0) — native flow fits: first tool call
  gets `tool_confirmation=None` and calls `request_confirmation`; ADK emits a long-running
  `adk_request_confirmation` call; the CLI answers with a `FunctionResponse` (same id,
  `{"confirmed", "payload"}`) and `invocation_id` of that event; ADK re-invokes the tool
  with the original args and the confirmation. Pending ids live in session state, the
  typed phrase is checked inside the tool (`execute_deletion`), not only in the CLI.
  Caveat: the model is called again right after the pause, so the tool result says
  explicitly that nothing is deleted yet.
- 2026-10-29: Bulk limit: more than 20 matches → the agent asks to narrow the selector
  (no second confirmation round).
