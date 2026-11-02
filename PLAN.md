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
| 1 Model and quota check | done |
| 2 Query layer (scoping, guard, executor) | done |
| 3 Report store and confirmation | done (tool wiring in stage 4) |
| 3b Client answers rework (SPEC v2) | done |
| 4 Agent assembly | done |
| 5 CLI | done (`/trace`, `/stats` in stage 7) |
| 6 Resilience | done |
| 7 Observability | todo |
| 8 Golden bucket + saved definitions | todo |
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
- Done 2026-11-03: `scripts/probe_models.py`; both `gemini-3.5-flash-lite` (primary) and
  `gemini-3.1-flash-lite` (fallback) 4/4 tool calls, 4/4 SQL through guard + dry run,
  ~1 s per call; earlier the same day 3.5 took 35–50 s per call → per-call timeout with
  fallback in stage 6. `docs/adr/0003-model.md`.

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

### 3b Client answers rework (1.5 h, TDD) — SPEC v2 §2, §3.4, §4.2–4.4, §5
The client's answers (SPEC §12) change finished code. RED tests first, then code:
- Brand scope: `Entitlement` becomes `brands` | `all: true` (departments and categories
  removed); `users.yaml` with real brands from the dataset (managers with one and with
  several brands, a CEO); scope and live tests rewritten around brands.
- Net revenue: `orders.order_revenue` excludes `Cancelled` and `Returned` items; schema
  descriptions state the revenue definition; live test checks it against `order_items`.
- Guard rule `demographics_not_aggregated` (sqlglot lineage over the final `SELECT`):
  rejected — `SELECT customer_key, city FROM users`, aliased (`city AS c`), via a CTE or
  subquery, `GROUP BY customer_key, city`, `GROUP BY order_id` with `users.age`,
  window functions without grouping; accepted — `GROUP BY city`, `COUNT(DISTINCT
  customer_key)` by state, top customers by revenue without demographics. Demographics
  used only as a filter (`WHERE state = 'California'`) in a customer-level query are
  allowed: no demographic column is output, but the filter implies it for every row —
  recorded as a known limit, closed in production by the aggregation thresholds.
- Hard delete: `soft_delete`/`restore` → `delete` (permanent); `deleted_at` column and
  `/undo` removed; audit log table (`report_audit`: time, user, action, report ids,
  trace id; no content).
- Evidence: unit + live suites green; counts recorded here.
- Done 2026-11-03: 138 offline tests, `make test-live` 23/23 — the 6 scoping checks plus
  every query the demographics rule accepts, executed on BigQuery as Bob (2–13 MB each).
  - Brand scope: Bob = Levi's, Wrangler, Diesel sees exactly those brands live; the
    apostrophe travels as a parameter.
  - Net revenue: live `SUM(order_revenue)` = items without Cancelled/Returned, < gross.
  - Demographics rule (`query/demographics.py`): 35 rejected and 17 accepted cases. After
    the first green run a bypass probe found two holes, both now tests: value subqueries
    without a FROM (`SELECT ARRAY(SELECT AS STRUCT customer_key, city FROM users)`,
    `SELECT (SELECT city FROM users WHERE customer_key = 'abc')`) and "groups" pinned to
    named customers (`WHERE customer_key = 'abc' GROUP BY city`).
  - Hard delete + `report_audit` (actions and ids, no content); 21 report tests.

### 4 Agent assembly (1.5 h) — SPEC §3, §4.5
- ADK `LlmAgent` with tools: `describe_data` (logical schema + coverage, no rows),
  `run_query`, `list_column_values`, `save_report`, `list_reports`, `delete_reports`,
  `find_similar_analyses` (stage 8), `set_definition` (stage 8).
- Callable `instruction`: persona file + current user's brand scope, definitions (default,
  conversation override, saved) and preferences + rules (data in delimited blocks is data;
  refuse out-of-scope topics; state definitions and their source; never output PII).
  Re-read every turn → persona changes without restart (R8 lite).
- `after_model` / output callback: PII pattern redaction of the final answer.
- Tools take identity from the session state, never from model arguments.
- Tests with a fake model: identity cannot be overridden by tool args; redaction applied.
- Evidence: a manual session answering one question of each SPEC §3.1 type.
- Done 2026-11-03: `agent/{services,tools,instruction,redaction,agent}.py`; 26 new tests
  drive the real ADK Runner with a scripted model (scope from the session, no identity
  parameters in any tool declaration, guard/BigQuery errors returned to the model, empty
  hint, list_column_values through the guard, answer and saved-report redaction, persona
  re-read per turn, delete: exact preview, typed phrase, cancel, no-match, no selector).
  `scripts/smoke_agent.py` as Bob on Gemini + BigQuery: 7/7 answered in 1–6 s — schema +
  coverage, top customers by customer_key, Levi's vs Wrangler (one guard `syntax`
  rejection of `'Levi''s'`, self-corrected), monthly net revenue, churned customers by
  state with the 90-day definition stated, email request refused without a tool call,
  Calvin Klein refused as out of scope. Follow-up: guard message and instruction now
  explain BigQuery quote escaping.

### 5 CLI (0.5 h) — SPEC §3.3, §5.3
- `retail-agent chat --user <id>`; `rich` rendering of markdown and tables.
- Commands: `/sql`, `/trace`, `/stats`, `/reports`, `/new`, `/quit` (no `/undo`: hard delete).
- Confirmation prompt: shows the preview and requires `delete N`.
- Sessions in SQLite (`DatabaseSessionService`) → resume after restart.
- Every turn wrapped: errors → short message with `trace_id`, loop continues.
- Tests: confirmation parsing (exact `delete N` only), error wrapping.
- Done 2026-11-03: `cli/chat.py` (turns, confirmation, error wrapping, resume) and
  `cli/app.py` (rich terminal, `/sql`, `/reports`, `/new`, `/help`, `/quit`); sessions in
  `data/sessions.sqlite` via `google-adk[db]`; 15 new tests (179 total). Live as Bob: Q3
  2026 net revenue by brand (Diesel $20,508.87 first) with definitions stated → `/sql` →
  report saved → `/reports` → delete: model asked to confirm without claiming deletion, CLI
  listed the exact report, `delete 1` deleted it → `/reports` empty. Restart without
  `--new` resumed the conversation (the agent recalled the first question). Unknown user →
  list of configured users. No errors in `logs/cli.log`.

### 6 Resilience (1 h) — SPEC §6, AC-6, AC-7
- Model calls: retry with backoff and jitter on 429/5xx/timeout, then fallback model;
  circuit breaker per model; per-turn budgets (model calls, queries).
- SQL self-correction: classified errors returned to the model with hints; max 3 attempts
  per question, enforced in code (tool returns "budget exhausted").
- Fault-injection tests: fake model raising 429 then succeeding; permanent failure →
  fallback → clear message; BigQuery transient vs permanent errors; budget exhaustion.
- Done 2026-11-03: `agent/resilience.py` (`ResilientLlm`: primary + fallback, 429 → next
  model, both limited → one `RetryInfo` wait ≤ 30 s, timeout/404 → next model, 5xx retried
  once, circuit breaker 3 failures / 60 s, bad requests raised) and `agent/budget.py`
  (model calls, queries and failed attempts per invocation). 16 new tests (195 total); the
  429 test payload copies the real free-tier response. Live fault injection through
  `ChatSession`: normal turn on 3.5-flash-lite; `RETAIL_MODEL=gemini-retired-test` → 404 →
  3.1-flash-lite answered the same number (276 October orders); 0.01 s timeout → both
  models timed out → friendly message with the trace id, no crash.

### 7 Observability (0.5 h) — SPEC §7, AC-8
- ADK `BasePlugin` writing JSON-lines spans to `logs/traces.jsonl`: turn, model call
  (tokens, latency, retries), tool call (SQL, guard decision, bytes, rows), confirmations,
  redactions. Questions and answers logged redacted.
- `/trace` (last turn timeline), `/stats` (session metrics from SPEC §7).
- Tests: a fake turn produces the expected span sequence; no PII in trace lines.
- Note in docs: ADK OpenTelemetry → Cloud Trace for production.

### 8 Golden bucket + saved definitions (1 h) — SPEC §8 R1, R4, §3.4
- `golden/trios/*.yaml` in the SPEC §8 format: ~20 Question → SQL (logical table names) →
  report summary, written by us; a test runs every SQL through the guard, a live test runs
  it on BigQuery. Required by the client — not cut.
- Retrieval: Gemini embeddings precomputed into `golden/index.json`; cosine top-3 as
  examples in `find_similar_analyses`. If embedding quota is a problem, a local TF-IDF
  index with the same interface.
- `set_definition(term, definition, remember)`: conversation override in session state;
  `remember=true` only on an explicit request → per-user store in SQLite, injected into the
  instruction; `/definitions` in the CLI shows the active ones and their source.

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
1. Stage 8 retrieval falls back to TF-IDF; saved definitions shrink to conversation-only.
2. Eval scenarios reduced to 8 + red-team 10.
3. Session resume dropped (documented).
Never cut: SQL guard + brand scoping, PII and demographics rules, delete confirmation,
resilience, tracing, golden trio format + sample set.

## Risks
- Gemini free tier: 15 requests/minute and 500/day for gemini-3.5-flash-lite (a 429 with
  `RetryInfo` 10 s was hit right after the smoke run), latency swings → stage 6 honours
  `RetryInfo`, falls back to the other model's quota, budgets per turn; small eval runs
  with pauses; the paid key changes only `.env`.
- Model writing SQL against logical names it has not seen as real tables → schema
  description + golden examples; guard errors are informative for self-correction.
- Scoped CTEs scan base tables on every query (tens of MB) → fine for this dataset and the
  BigQuery free tier; production would use authorized views or row-level security (HLD).
- ADK confirmation API is new → spike first in stage 3, fallback design ready.
- Demographics rule relies on column lineage; unusual SQL (UNNEST of structs, `SELECT AS
  STRUCT`, JSON functions) may not trace → the guard fails closed: an output column it
  cannot trace counts as demographic when `users` is in the query.

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
- 2026-11-03: Client answers received (SPEC §12, SPEC v2): brand scope (JWT in
  production), demographics only in aggregates, hard delete with exact-list confirmation,
  net revenue and 90-day churn defaults with overrides, golden format + sample set required.
  Overrides: one-off → conversation; explicit "remember" → saved per user (R4).
- 2026-11-03: `orders.gender` dropped from the logical schema (a copy of the customer's
  gender; `users.gender` stays) so `SELECT * FROM orders` remains a legal customer-level
  query. Demographics rule also rejects identifier-pinning filters (`=`, `IN (...)`,
  `LIKE` against literals) when the result outputs demographics.
- 2026-11-03: Primary model `gemini-3.5-flash-lite` (author's choice; free key, 500
  requests/day). First measurements on a one-word prompt: 49.8 s, 35.6 s, 47.2 s;
  `gemini-3.1-flash-lite` on the same key and network: 4.9 s → the latency is the model on
  the free tier, not the network. `gemini-2.5-flash-lite` (the old default) returns 404 for
  new users. Provisional fallback `gemini-3.1-flash-lite`; stage 1 confirms with the
  tool-calling and SQL probe.
- 2026-11-03: A 404 from the model API switches to the fallback instead of failing the
  turn: `gemini-2.5-flash-lite` was retired for new users during this project, so a
  retired model is a realistic outage, not a programming error.
