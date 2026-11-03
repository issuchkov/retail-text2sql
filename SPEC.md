# SPEC — Retail Data Analysis Chat Agent

Status: v2. The client's answers to the open questions (2026-11-03, §12) are folded in.
Written before any code; behaviour changes start here.
Client: OpsFleet (Team Lead + Harry, project managers). Dataset:
`bigquery-public-data.thelook_ecommerce` (`orders`, `order_items`, `products`, `users`).

## 1. Purpose and scope

A chat agent that lets non-technical executives of a retail company ask analysis questions
in natural language, discuss the answers, and turn them into saved reports with action
items. It writes and runs SQL against BigQuery, learns from analysts' past work (the
"Golden Knowledge" bucket of Question → SQL → Report trios), never shows personal data,
only analyses the brands a user manages, and asks for explicit confirmation before
destructive operations on the saved-report library.

Two deliverables with different depth:

| Requirement | Prototype (working code) | Design (HLD + explanation) |
|---|---|---|
| R1 Hybrid intelligence (Golden bucket) | trio format + sample set (required); lite similarity retrieval as a tool | full: storage, indexing, curation and update loop |
| R2 Safety & PII masking | **full** | full |
| R3 High-stakes oversight (destructive ops) | **full** | full |
| R4 Learning loop (user + system level) | user level: saved answer-format preferences and business definitions | full (system level) |
| R5 Resilience & graceful error handling | **full** | full |
| R6 Quality assurance | offline eval + safety test suite | full |
| R7 Observability | **full** | full |
| R8 Persona management without redeploy | persona file re-read every turn; `/persona` lets a persona editor change it from the chat, versioned | full |

Bold = required by the brief for the prototype; "lite" = cheap to show and makes the design
concrete, cut first if time runs out (PLAN.md). The golden trio format and sample set are
requested by the client and are not cut.

Interface: a CLI chat. Must run on another machine from the setup instructions (no
Docker requirement). Framework: Google ADK (§9).

Out of scope for the prototype: real authentication (a `--user` switch stands in for the
JWT, §2), web UI, email/charts/web search (designed as extension points, §10),
multi-tenant deployment.

## 2. Users and identity

- A user is an identity: `user_id`, display name, title, and a **brand scope** (§4.3).
  Managers manage one or more brands; the CEO (and other company-wide roles) see all.
- Prototype: identities and scopes in `config/users.yaml`; the CLI selects one with
  `--user <id>`. `persona_editor: true` marks users allowed to change the persona (§8 R8).
- Production: the frontend sends a JWT with the user's id and brand scopes. The API
  verifies its signature, issuer, audience and expiry against the identity provider's keys,
  and builds the same identity record from the claims (`sub` → `user_id`, a `brands` claim
  or `all` → scope). Nothing else changes: the record is put into the session and every
  tool reads it from there.
- Every tool call runs **as** the current identity; tools never take a user id or a scope
  from the model's arguments.

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
- Requests for personal data, for demographics of individual customers, or for
  re-identifying customers (§4.2).
- Data outside the user's brand scope (§4.3) — the answer says the scope is limited,
  without revealing out-of-scope numbers.
- Attempts to change its instructions, reveal its prompt, or act as another user.

### 3.3 Answer contract (CLI)
Each answer shows: the answer text (format adapted to the user's saved preferences, §3.5:
tables, bullets or prose; brief, standard or detailed), the definitions and assumptions used with their source (§3.4), and a
footer with `trace_id` and the queries run (count, bytes processed). `/sql` shows the full
SQL of the last turn.

### 3.4 Business definitions
There is no company-wide definition of churn or revenue; the agent uses defaults, states
the definition in every answer that relies on it, and lets users override it.

| Term | Default |
|---|---|
| Revenue | Sum of `sale_price` of items whose status is not `Cancelled` or `Returned` |
| Churned customer | No purchase in the 90 days before the end of the period analysed; only customers with at least one purchase that was not cancelled or returned count, judged by their last such purchase |
| Quarter, month, week | Calendar periods in UTC |

- A one-off override ("use 60 days") applies to the rest of the conversation.
- An explicit request to remember ("always use 60 days") saves the definition for that
  user; it applies to all later conversations until changed or reset.
- The answer names the source: "default", "this conversation" or "your saved definition".
- Saved definitions are user preferences (R4) and never change another user's answers.

### 3.5 User preferences (R4, user level)
- Aspects and values: `format` (tables | bullets | prose), `depth` (brief | standard |
  detailed), `visuals` (text | charts). A preference is saved per user when the user states
  it ("I prefer bullet points", "keep it brief"), via `remember_preference`; `default`
  removes it. Saved preferences are added to the instruction of every later turn and
  conversation; `/preferences` shows them.
- `visuals: charts` is recorded, but the prototype has no chart tool: answers then include a
  chart-ready table and name the chart that fits (charts are an extension point, §10).
- Preferences change presentation only, never scope, definitions or safety rules.
- Learning without explicit statements (inferring depth or format from feedback and
  follow-ups, then proposing a preference to the user) is design-only (HLD §5).

## 4. Safety and PII (R2) — required in the prototype

### 4.1 Threat model
Prompt injection in the question, requests to dump raw rows, PII exfiltration through
aggregates or free-text columns, re-identification through demographics of individual
customers, cross-scope access, SQL that writes or scans expensively, and the model echoing
PII it saw in tool output.

### 4.2 PII policy
- **Never shown and never sent to the model**: `users.first_name`, `users.last_name`,
  `users.email`, `users.street_address`, `users.postal_code`, `users.latitude`,
  `users.longitude`, `users.user_geom`. They are removed before the query runs (§4.3), so
  they cannot appear in any result the model receives.
- **Customer identifiers** (`users.id`, `orders.user_id`, `order_items.user_id`) are
  replaced by a stable pseudonym `customer_key` (salted hash). Customer-level analysis by
  `customer_key` is allowed ("top 10 customers by revenue").
- **Demographics only in aggregates**: `users.age`, `gender`, `city`, `state`, `country`,
  `traffic_source` may appear in a result only as a group key or
  inside an aggregate of a query that groups rows, and never in a result whose rows are
  individual customers, orders or items (§4.4 rule "demographics_not_aggregated").
  Using a demographic only as a filter in a customer-level query ("top customers in
  California by revenue") is allowed: no demographic is output, but the filter implies it
  for every row — a known limit, closed in production by minimum group sizes.
- Everything in `orders`, `order_items`, `products` is allowed apart from the above.
- Small-group protection (design only): a group of very few customers can still point to a
  person. Production enforces a minimum group size with BigQuery aggregation threshold
  rules on the customer view (HLD); the prototype does not.

### 4.3 Brand scope ("each user only analyses products related to him")
- A manager's scope is a list of brands; company-wide roles have `all: true`.
- Enforced in the query layer, not by the prompt: the model writes SQL against the
  logical names `orders`, `order_items`, `products`, `users`. Before execution the server
  binds these names to **scoped, PII-free CTEs** built from the base tables:
  - `products` = products of the user's brands;
  - `order_items` = items of those products (with `customer_key`);
  - `orders` = orders that contain at least one in-scope item; `num_of_item` is the number
    of in-scope items, `order_revenue` their revenue as defined in §3.4; the base table's
    copy of the customer's `gender` is not exposed (use `users.gender`);
  - `users` = customers who bought in-scope products, PII columns removed, `customer_key`
    instead of `id`.
  Customer and order data are therefore visible only through purchases of in-scope
  products. The model never sees base table names and cannot reference them. Brand values
  and the pseudonym salt are BigQuery query parameters, never spliced into SQL text.

### 4.4 Query guard (deterministic, before BigQuery)
Parsed with a SQL parser (sqlglot, BigQuery dialect). A query is rejected unless:
- single statement; `SELECT`/`WITH` only (no DML, DDL, scripting, `EXPORT`, `CALL`);
- every table reference is one of the four logical names or a CTE defined in the query; no
  fully-qualified or wildcard tables;
- no reference to a PII column or a raw customer id (`user_id`);
- no query parameters and no identity, external or ML functions (`SESSION_USER`,
  `EXTERNAL_QUERY`, `ML.*`, …);
- **demographics_not_aggregated**: if an output column of the final result derives from a
  demographic column (traced through aliases, expressions, subqueries and CTEs), the final
  `SELECT` must group (`GROUP BY` or aggregate functions), and neither its group keys nor
  its output columns may derive from a row identifier (`customer_key`, `order_id`,
  `order_items.id`, `inventory_item_id`). In such a query a row identifier may appear
  anywhere only as a plain column that is output or grouped by in an inner step, counted
  (`COUNT`, `COUNT(DISTINCT …)`), compared for equality with another identifier column (a
  join key), or matched against a subquery of identifiers (`IN (SELECT customer_key …)`).
  Any other use — comparing it with a value (`=`, `<`, `BETWEEN`, `IN (…)`,
  `IN UNNEST(…)`), passing it to a function (`STARTS_WITH`, `REGEXP_CONTAINS`, `SUBSTR`,
  `CAST`), or using it in a condition inside an aggregate (`COUNTIF(customer_key = …)`) —
  could single out one customer and is rejected. (An independent review found seven such
  bypasses of an earlier rule that only looked at `=`, `IN` and `LIKE` in filters.) A
  subquery or CTE that produces identifiers and is cut by `LIMIT` below 5 rows ("the top
  customer") is rejected too. Narrowing a group by value thresholds (`revenue > X`) to a
  handful of customers is **not** caught: minimum group sizes are enforced only in
  production (aggregation thresholds, §4.2).
- a `LIMIT` is added if absent and capped at the maximum (default 1,000 rows).

Then a **dry run** must pass and estimate under the byte cap (`maximum_bytes_billed`,
default 1 GB per query) before the real run.

### 4.5 Output guard
Tool results returned to the model are capped (rows and characters) and scanned; the final
answer is scanned before display for PII patterns (emails, phone numbers, street
addresses, exact coordinates) and redacted (`[redacted]`), with the event logged. Data
from the database is passed to the model as structured tool results (JSON function
responses), never as instructions, and the instruction tells the model to treat them as
data. A result that hits the row cap is marked `row_limit_reached` (it may or may not be
complete), so the model aggregates further instead of assuming completeness.

**Grounded figures:** every amount or count of 1,000 or more in the final answer (years
excepted) must be traceable to a query result in the conversation — a value, a column
total, or the sum or difference of two values, allowing for rounding. Otherwise the answer
is shown with a note that those figures are unverified, and the turn's trace records them.
(Eval run 1 found an answer with an invented revenue figure and no query.)

## 5. Saved reports and destructive operations (R3) — required

### 5.1 Library
Reports are stored with: `report_id`, `owner_id`, `conversation_id`, title, body
(markdown), entities mentioned (brands, categories, states, …), created/updated time.
Only the author can see, list or delete a report. Prototype store: SQLite file.
Production: Cloud SQL (PostgreSQL) with owner checks in every query and row-level
security, backups under a retention policy (HLD).

### 5.2 Tools
- `save_report(title, body, entities)` — saves under the current user and conversation.
- `list_reports(selector)` — the user's own reports only.
- `get_report(report_id)` — the full text of one of the user's own reports, so it can be
  reopened and discussed; `/report <id>` in the CLI shows it without a model call.
- `delete_reports(selector)` — selector: `ids` | `mentions: <text>` | `this_conversation`
  | `older_than: <date>` (criteria combine with AND). "Client X" = any entity named in a
  report's title, body or entities.

### 5.3 Confirmation flow (hard delete)
1. The agent resolves the selector to concrete reports **owned by the user** (others'
   reports are never matched, and the answer does not reveal they exist).
2. If nothing matches → says so; no confirmation asked.
3. More than 20 matching reports → nothing is previewed; the agent asks the user to
   narrow the selector. One deletion waits for confirmation at a time: a second delete
   request while one is pending is refused with a message (confirm or cancel the first,
   then ask again), so no request is silently dropped.
4. Otherwise the tool pauses with a confirmation request (ADK tool confirmation) showing
   the exact list: count, ids, titles, dates. Nothing is deleted at this point and the
   agent says so.
5. The CLI asks the user to type `delete N` (N = the number shown). Anything else cancels.
   The phrase is checked by the server-side tool, not only by the CLI; the model cannot
   confirm on the user's behalf.
6. On confirmation the server **permanently deletes** exactly the previewed ids (not a
   re-evaluated selector) that still exist and belong to the user, and reports what was
   deleted. There is no undo.
7. An audit log records every request, preview, confirmation, cancel and deletion with
   `trace_id`, user and report ids (no report content).

## 6. Resilience (R5) — required

- **SQL self-correction:** guard rejections, dry-run errors and execution errors are
  classified (syntax/semantic/guard → returned to the model with the error and a hint;
  empty result → returned with a hint to check filters and value spellings, with a tool to
  list distinct values of a column; resource/permission → not retried by the model). At
  most 3 correction attempts per question, then the agent explains what it tried.
- **Cost bounds per turn:** max model calls (default 12), max queries (default 8), byte
  cap per query; when a budget is reached the agent answers with what it has.
- **Model failures:** a 429 switches to the fallback model at once (separate quota); when
  both are rate-limited, one wait for the advertised `RetryInfo` delay if it is at most 30 s;
  a timeout (default 25 s) or a 404 (retired model) switches to the fallback; 5xx and
  network errors are retried once with backoff and jitter; a circuit breaker per model
  skips a model after 3 consecutive failures for 60 s; other 4xx errors are not retried.
- **BigQuery failures:** transient errors retried; outage → clear message, the
  conversation continues.
- **CLI never crashes:** every turn is wrapped; errors become a short user message with the
  `trace_id`; state is persisted so the conversation resumes after a restart.
- **Conversation length:** before each model call, tool results from earlier turns are
  compacted to their metadata (status, columns, row count, rule or error) — the answers
  written from them stay in the history — so long conversations stay bounded in tokens and
  cost. If the model still rejects the request as too long, the user is told to start a new
  conversation with `/new` instead of seeing a generic error.

## 7. Observability (R7) — required

- **Trace per turn** (`trace_id`): user message (redacted), each model call (model,
  tokens, latency, finish reason, retries), each tool call (name, arguments, SQL,
  guard decision, rows, bytes processed and billed, latency, errors), confirmations,
  output-guard redactions and unverified figures, the persona version, and the turn status.
  Prototype: JSON lines in `logs/traces.jsonl`.
- **Turn status:** `answered` (at least one query succeeded), `no_query` (answered without
  querying — refusals, schema questions, report management, definitions, preferences), `queries_failed` (every
  query was rejected or failed), `unverified_figures`, `budget_exhausted`,
  `awaiting_confirmation`, `error`.
- **CLI:** `/trace` shows the last turn's timeline; `/stats` shows session metrics;
  `/feedback good|bad [note]` records the user's rating of the last answer in the trace.
  Traces hold metadata and the redacted user message — no answer text and no result rows;
  every string is redacted.
- **Metrics:** turns by status; data-answer success rate (`answered` among turns that tried
  to answer with data); share of turns answered without data (refusals and meta
  questions); SQL first-try success and self-correction rate, empty-result rate, guard
  rejections by rule, PII redactions, unverified figures, model errors/fallbacks, latency
  p50/p95 per turn and per step, tokens and BigQuery bytes processed and billed per turn
  (cost), confirmation outcomes (confirmed/cancelled), user feedback.
- **Deep dive:** traces say *what* happened; the full message correspondence of a turn —
  user messages, model messages, tool calls with arguments and results, confirmations — is
  kept in the session store, and `scripts/show_conversation.py <trace_id | session_id>`
  prints it next to the trace. Session data holds no PII columns (they never leave
  BigQuery) and stays local in the prototype.
- Production: OpenTelemetry (ADK emits spans) → Cloud Trace; logs → Cloud Logging → BigQuery
  sink; dashboards and alerts (HLD); sessions in Cloud SQL with access limited to the
  support role and a retention period.

## 8. Golden bucket, learning, QA, persona

- **R1 Golden bucket.**
  - Format (prototype, `golden/trios/*.yaml`): `id`, `question`, `intent` (one line),
    `sql` (logical table names), `report` (how an analyst summarised the result: structure
    and kind of insight, **no figures**), `definitions` (terms and the definitions used),
    `tags` (metrics, dimensions), `brands` (brands the trio is about; empty = generic),
    `author`, `status` (`seed` | `reviewed`), `reviewed_at`.
  - Scope: a trio is returned only to users whose scope covers all its `brands`, and
    reports carry no figures, so examples never leak out-of-scope numbers.
  - Sample set: about 20 trios covering the SPEC §3.1 question types; every `sql` passes
    the guard and runs on BigQuery. The prototype's trios are `seed` (written for the
    demo, not yet reviewed by an analyst).
  - Retrieval (prototype, lite): embeddings of question, intent and tags precomputed into
    `golden/index.json`; at query time `find_similar_analyses` embeds the question and
    returns up to 3 in-scope trios above a minimum similarity (embedding cosine 0.70,
    lexical 0.10 — calibrated: off-topic questions scored at most 0.671, the weakest
    on-topic match 0.698) as examples of interpretation, SQL and report style — examples,
    not answers; below it the tool says there is no close example. Trios with status
    `retired` are never returned. If embedding fails, a local lexical (TF-IDF) ranking is
    used; a stale index is logged.
  - Design: storage in GCS (one file per trio, versioned), indexed into a vector store with
    metadata filters (tags, brands); hybrid retrieval (vector + keyword); update loop:
    saved reports and positive feedback → candidate trios → analyst review → index;
    stale or failing trios retired by a nightly job that re-runs their SQL.
- **R4 Learning loop:** user level — saved answer-format preferences (§3.5) and business
  definitions (§3.4) from explicit requests, stored per user, injected into instructions
  (prototype);
  system level (design) — failures, corrections and feedback mined from traces into new
  golden trios, prompt and eval updates.
- **R6 QA:** offline eval set (questions with reference SQL run at eval time and report
  criteria), execution accuracy, LLM-as-judge rubric for intent coverage, red-team safety
  set, UX evaluation (task success, turns to answer, feedback).
- **R8 Persona:** tone/instructions stored outside the code and re-read every turn. In the
  prototype a persona editor (the CEO in `users.yaml`) runs `/persona` to see it and
  `/persona set <text>` to replace it from the chat — no developer, restart or redeploy; the
  previous version is kept in `data/persona_history/`, and `/persona history` lists the
  versions. Persona text only sets tone: safety rules, scope and definitions are added by
  code and take precedence; the rules come first in the instruction. Production: a versioned config store with an edit UI, preview against the
  eval suite, staged rollout and rollback.

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
| AC-2 | No PII column or PII pattern ever appears in a tool result, an answer, a saved report or a trace | unit tests (guard), red-team set, trace scan |
| AC-3 | A user cannot obtain metrics for brands outside their scope | unit tests on the scoping layer + live tests + red-team prompts |
| AC-4 | Non-SELECT, multi-statement, base-table, wildcard and PII-column queries are rejected before BigQuery | unit tests |
| AC-5 | Deletes show the exact list, require the typed phrase, and permanently delete exactly the previewed own reports | unit + CLI scenario tests |
| AC-6 | A SQL error or empty result triggers at most 3 correction attempts, within the turn budget | tests with a fake model + live scenario |
| AC-7 | Model or BigQuery outage yields a clear message, no crash, conversation continues | fault-injection tests |
| AC-8 | Every turn has a trace with model/tool steps, tokens, bytes and errors; `/trace` and `/stats` work | tests + manual |
| AC-9 | Runs on a clean machine from the README instructions | fresh-clone run |
| AC-10 | Demographics never appear for individual customers, orders or items; aggregates by them work | unit tests (guard) + red-team prompts |
| AC-11 | Answers using churn or revenue state the definition and its source; overrides apply for the conversation, saved ones in later conversations | tests with a fake model + live scenario |
| AC-12 | Golden sample trios pass the guard and run; retrieval returns relevant examples | unit + live tests |
| AC-13 | A stated format preference is saved per user and shapes later answers in new conversations; other users are unaffected | tests with a fake model + live scenario |
| AC-14 | A persona editor changes the persona from the chat and the next turn uses it; other users cannot | unit tests + live run |
| AC-15 | Given a trace id, the full correspondence of that turn can be printed | unit test + manual |

## 12. Client answers (2026-11-03)

| # | Question | Answer → where it lands |
|---|---|---|
| A1 | What links an executive to "their" products? | Managers manage one or more brands; the CEO sees all; customers and orders only through in-scope purchases; mapping in config; production JWT with brand scopes → §2, §4.3 |
| A2 | Which fields are PII; are customer-level answers allowed? | Never show names, email, street address, postal code, exact coordinates; city, state, country, age, gender, traffic source only in aggregates; customer-level analysis by internal id or a salted pseudonym is fine; PII must never reach the LLM → §4.2, §4.4 |
| A3 | Golden bucket contents | None to share; we define the format and a small sample set; the design explains updates and retrieval → §8 R1 |
| A4 | Saved-report storage; what is "Client X"? | SQLite for the demo, production approach in the HLD; "Client X" = any entity named in a report; strict confirmation showing the exact list before a hard delete; only the author sees or deletes → §5 |
| A5 | Identity in the CLI | CLI selector for the prototype; production identity and scopes from a JWT, documented → §2 |
| A6 | Definitions: churn, quarters | No company-wide churn definition: sensible default (no purchase in 90 days; revenue excludes cancelled and returned items), stated in the answer, overridable; calendar quarters in UTC → §3.4 |
| A7 | Model constraints | Our assumption: Gemini Flash family; free tier in development |
| A8 | Data freshness | Our assumption: dataset is synthetic and refreshed daily; "last month" is relative to today |
