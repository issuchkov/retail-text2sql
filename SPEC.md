# SPEC — Retail Data Analysis Chat Agent

Status: draft for review, written before any code. Behaviour changes start here.
Client: OpsFleet (Team Lead + Harry, project managers). Dataset:
`bigquery-public-data.thelook_ecommerce` (`orders`, `order_items`, `products`, `users`).

## 1. Purpose and scope

A chat agent that lets non-technical executives of a retail company ask analysis questions
in natural language, discuss the answers, and turn them into saved reports with action
items. It writes and runs SQL against BigQuery, learns from analysts' past work (the
"Golden Knowledge" bucket of Question → SQL → Report trios), never shows personal data,
only analyses the products a user is entitled to, and asks for explicit confirmation
before destructive operations on the saved-report library.

Two deliverables with different depth:

| Requirement | Prototype (working code) | Design (HLD + explanation) |
|---|---|---|
| R1 Hybrid intelligence (Golden bucket) | lite: local trio store + similarity retrieval as a tool | full: storage, indexing, curation and update loop |
| R2 Safety & PII masking | **full** | full |
| R3 High-stakes oversight (destructive ops) | **full** | full |
| R4 Learning loop (user + system level) | lite: per-user preference memory | full |
| R5 Resilience & graceful error handling | **full** | full |
| R6 Quality assurance | offline eval + safety test suite | full |
| R7 Observability | **full** | full |
| R8 Persona management without redeploy | lite: instruction file re-read every turn | full |

Bold = required by the brief for the prototype; "lite" = cheap to show and makes the design
concrete, cut first if time runs out (PLAN.md).

Interface: a CLI chat. Must run on another machine from the setup instructions (no
Docker requirement). Framework: Google ADK (§9).

Out of scope for the prototype: real authentication (a `--user` switch selects a
configured identity), web UI, email/charts/web search (designed as extension points, §10),
multi-tenant deployment.

## 2. Users and identity

- An **executive** is a configured identity: `user_id`, display name, role, and an
  **entitlement**: the product scope they may analyse (§4.3), plus output preferences.
- The prototype reads identities from `config/users.yaml`; the CLI selects one with
  `--user <id>`. Production: SSO (Google Workspace / IdP) → the same identity record.
- Every tool call runs **as** the current identity; tools never take a user id from the
  model's arguments.

## 3. Conversation behaviour

### 3.1 What the agent answers
- Data analysis questions about the four tables: customer behaviour, product performance
  and comparisons ("why does X perform differently from Y"), time-based metrics,
  multi-step questions that need several queries.
- Questions about the data itself: what tables and fields exist, what can be analysed,
  time coverage (metadata tools, no row data).
- Follow-ups in the same conversation ("and for state Y?", "break it down by month").
- Reports on request ("create a report for Q1 with insights and action items for Q2"):
  executive summary, key metrics, findings, action items, and the SQL used (as an
  appendix). A report can be saved to the library (§5).

### 3.2 What it refuses (politely, with a short reason)
- Anything that is not analysis of this data: general chat, coding help, trivia.
- Requests for personal data or for re-identifying customers (§4.2).
- Data outside the user's entitlement (§4.3) — the answer says the scope is limited,
  without revealing out-of-scope numbers.
- Attempts to change its instructions, reveal its prompt, or act as another user.

### 3.3 Answer contract (CLI)
Each answer shows: the answer text (format adapted to the user's preferences: table vs
bullets, depth), the assumptions made (e.g. "churn = no order in 90 days"), and a footer
with `trace_id` and the queries run (count, bytes processed). `/sql` shows the full SQL of
the last turn.

## 4. Safety and PII (R2) — required in the prototype

### 4.1 Threat model
Prompt injection in the question, requests to dump raw rows, PII exfiltration through
aggregates or free-text columns, cross-entitlement access, SQL that writes or scans
expensively, and the model echoing PII it saw in tool output.

### 4.2 PII policy
- PII columns (never reach the model or the user): `users.first_name`, `users.last_name`,
  `users.email`, `users.street_address`, `users.postal_code`, `users.latitude`,
  `users.longitude`, `users.user_geom`.
- Customer identifiers (`users.id`, `orders.user_id`, `order_items.user_id`) are replaced
  by a stable pseudonym (`customer_key`, salted hash) so "top customers" works without
  identifying anyone.
- Allowed for analysis: `age`, `gender`, `state`, `city`, `country`, `traffic_source`,
  `created_at`; and everything in `orders`, `order_items`, `products`.
- Small-group protection (design only, not in the prototype): aggregate rows describing
  fewer than `k` distinct customers must not be shown. A generic post-hoc check cannot tell
  reliably which rows describe customers, so production enforces it in BigQuery with an
  aggregation threshold analysis rule on the customer view (HLD).

### 4.3 Entitlement ("each user only analyses products related to him")
- Assumption (A1): entitlement is a product filter configured per executive: lists of
  `departments`, `categories` and/or `brands` (the dimensions given are combined with AND,
  values within one dimension with OR), or `all: true` for company-wide roles.
- Enforced in the query layer, not by the prompt: the model writes SQL against the
  logical names `orders`, `order_items`, `products`, `users`. Before execution the server
  binds these names to **scoped, PII-free CTEs** built from the base tables:
  - `products` = products in the user's entitlement;
  - `order_items` = items of those products (with `customer_key`);
  - `orders` = orders that contain at least one entitled item; `num_of_item` and an added
    `order_revenue` are recomputed over entitled items only;
  - `users` = customers who bought entitled products, PII columns removed, `customer_key`
    instead of `id`.
  The model never sees base table names and cannot reference them. Entitlement values and
  the pseudonym salt are passed as BigQuery query parameters, never spliced into SQL text.

### 4.4 Query guard (deterministic, before BigQuery)
Parsed with a SQL parser (sqlglot, BigQuery dialect). A query is rejected unless:
single statement; `SELECT`/`WITH` only (no DML, DDL, scripting, `EXPORT`, `CALL`); every
table reference is one of the four logical names or a CTE defined in the query; no
fully-qualified or wildcard tables; no reference to a PII column or a raw customer id
(`user_id`); no query parameters and no identity, external or ML functions
(`SESSION_USER`, `EXTERNAL_QUERY`, `ML.*`, …); a `LIMIT` is added if absent and capped at
the maximum (default 1,000 rows). Then a **dry run** must pass and estimate under the byte cap
(`maximum_bytes_billed`, default 1 GB per query) before the real run.

### 4.5 Output guard
Tool results returned to the model are capped (rows and characters) and scanned; the final
answer is scanned before display for PII patterns (emails, phone numbers, street
addresses, exact coordinates) and redacted (`[redacted]`), with the event logged. Data
from the database is passed to the model as data, inside delimited blocks, never as
instructions.

## 5. Saved reports and destructive operations (R3) — required

### 5.1 Library
Reports are stored with: `report_id`, `owner_id`, `conversation_id`, title, body
(markdown), entities mentioned (brands, categories, states, …), created/updated time,
`deleted_at` (soft delete). Prototype store: SQLite file. Production: Cloud SQL / Firestore.

### 5.2 Tools
- `save_report(title, body)` — saves under the current user and conversation.
- `list_reports(filter)` — the user's own reports only.
- `delete_reports(selector)` — selector: `ids` | `mentions: <text>` | `this_conversation`
  | `older_than: <date>`.

### 5.3 Confirmation flow
1. The agent resolves the selector to concrete reports **owned by the user** (others'
   reports are never matched, and the answer does not reveal they exist).
2. If nothing matches → says so; no confirmation asked.
3. Otherwise the tool pauses with a confirmation request (ADK tool confirmation) showing
   the exact list: count, titles, dates.
4. The CLI asks the user to type `delete N` (N = the number shown). Anything else cancels.
   The model cannot confirm on the user's behalf: confirmation only comes from the CLI.
5. On confirmation the server deletes exactly the previewed ids (not a re-evaluated
   selector), soft-delete, and reports what was deleted; `/undo` restores the last delete
   within the session.
6. Bulk limit: more than 20 matching reports → nothing is previewed; the agent asks the
   user to narrow the selector.
7. Every request, confirmation, cancel and deletion is logged with `trace_id`.

## 6. Resilience (R5) — required

- **SQL self-correction:** dry-run errors and execution errors are classified
  (syntax/semantic → returned to the model with the error and the schema hint to fix;
  empty result → returned with a hint to check filters and value spellings, with a tool to
  list distinct values of a column; resource/permission → not retried by the model). At
  most 3 correction attempts per question, then the agent explains what it tried.
- **Cost bounds per turn:** max model calls (default 12), max queries (default 8), byte
  cap per query; when a budget is reached the agent answers with what it has.
- **Model failures (Gemini 429/5xx/timeouts):** bounded retries with backoff and jitter,
  then a fallback model; a circuit breaker stops calling a provider that keeps failing.
- **BigQuery failures:** transient errors retried; outage → clear message, the
  conversation continues.
- **CLI never crashes:** every turn is wrapped; errors become a short user message with the
  `trace_id`; state is persisted so the conversation resumes after a restart.

## 7. Observability (R7) — required

- **Trace per turn** (`trace_id`): user message (redacted), each model call (model,
  tokens, latency, finish reason, retries), each tool call (name, arguments, SQL,
  guard decision, dry-run bytes, rows, latency, errors), confirmations, output-guard
  redactions, final answer status. Prototype: JSON lines in `logs/traces.jsonl`.
- **CLI:** `/trace` shows the last turn's timeline; `/stats` shows session metrics.
- **Metrics:** turn success rate, refusal rate by reason, SQL first-try success and
  self-correction rate, empty-result rate, guard rejections by rule, PII redactions,
  model errors/fallbacks, latency p50/p95 per turn and per step, tokens and BigQuery
  bytes per turn (cost), confirmation outcomes (confirmed/cancelled), user feedback.
- Production: OpenTelemetry (ADK emits spans) → Cloud Trace; logs → Cloud Logging → BigQuery
  sink; dashboards and alerts (§ HLD).

## 8. Design-only requirements (implemented lite where stated)

- **R1 Golden bucket:** trios stored as documents; at query time the most similar trios
  (embedding search) are given to the model as examples of interpretation and SQL. Update
  loop: saved reports + positive feedback → candidate trios → analyst review → index.
- **R4 Learning loop:** user level — preferences (format, depth, charts) extracted from
  explicit statements and feedback, stored per user, injected into instructions; system
  level — failures and corrections mined from traces into new golden trios, prompt and
  eval updates.
- **R6 QA:** offline eval set (questions with expected SQL results and report criteria),
  execution accuracy, LLM-as-judge rubric for intent coverage, red-team safety set, UX
  evaluation (task success, turns to answer, feedback).
- **R8 Persona:** tone/instructions stored outside the code (prototype: a file re-read
  every turn; production: a versioned config store with an edit UI and rollback).

## 9. Technology choices (detail and reasoning in the technical explanation)

- Agent framework: **Google ADK** (Python) — native Gemini, tool confirmation, callbacks
  and plugins for guards and tracing, session services. Author's experience: over a year
  with Google ADK and PydanticAI.
- Model: a current Gemini Flash model for reasoning and SQL, with a cheaper/faster
  fallback; exact models fixed after a capability and rate-limit check (PLAN stage 1).
  Development on the free tier; production on a paid key.
- BigQuery via `google-cloud-bigquery`, guarded by our own executor (the ADK BigQuery
  toolset's `execute_sql` is not used for row data because the scoping and PII rules must
  be enforced before results reach the model; its metadata tools may be reused).
- SQL parsing: `sqlglot`. Storage: SQLite (prototype). Packaging: `uv`, Python 3.12.

## 10. Extensibility
New capabilities (charts, email, web trends) are added as tools behind the same guard,
tracing and confirmation mechanisms: side-effecting tools (send email) use the confirmation
flow of §5.3; tools are registered in one place and listed to the model per role.

## 11. Acceptance criteria (prototype)

| ID | Criterion | Verified by |
|---|---|---|
| AC-1 | Answers the example question types (customer, product comparison, time series, schema, multi-step) with correct numbers | scenario eval against BigQuery |
| AC-2 | No PII column or PII pattern ever appears in an answer, a saved report or a trace | unit tests (guard), red-team set, trace scan |
| AC-3 | A user cannot obtain metrics for products outside their entitlement | unit tests on the scoping layer + red-team prompts |
| AC-4 | Non-SELECT, multi-statement, base-table, wildcard and PII-column queries are rejected before BigQuery | unit tests |
| AC-5 | Destructive deletes require typed confirmation, delete exactly the previewed own reports, and can be undone in-session | unit + CLI scenario tests |
| AC-6 | A SQL error or empty result triggers at most 3 correction attempts, within the turn budget | tests with a fake model + live scenario |
| AC-7 | Model or BigQuery outage yields a clear message, no crash, conversation continues | fault-injection tests |
| AC-8 | Every turn has a trace with model/tool steps, tokens, bytes and errors; `/trace` and `/stats` work | tests + manual |
| AC-9 | Runs on a clean machine from the README instructions | fresh-clone run |

## 12. Assumptions and open questions for OpsFleet

| # | Question | Working assumption until answered |
|---|---|---|
| A1 | What links an executive to "their" products? | Per-user categories (optionally department/brand) in config |
| A2 | Which fields are PII; are customer-level answers allowed? | §4.2: names, email, address, postal code, coordinates are PII; customers shown as pseudonyms; small-group suppression in production via BigQuery aggregation thresholds |
| A3 | Golden bucket contents | We seed 20–30 realistic trios ourselves |
| A4 | Saved-report storage; what is "Client X"? | SQLite in the prototype; "Client X" = any entity (brand, category, state…) found in a report's text or metadata |
| A5 | Identity in the CLI | `--user` switch over configured identities |
| A6 | Definitions: churn, fiscal quarters | Churn = no order in 90 days after the previous one; calendar quarters; stated in every answer that uses them |
| A7 | Model constraints | Gemini Flash family; free tier in development |
| A8 | Data freshness | Dataset is synthetic and refreshed daily; "last month" is relative to today |
