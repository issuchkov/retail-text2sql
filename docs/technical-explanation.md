# Technical explanation

How each requirement is met, which technology was chosen and why, and where the prototype
stops. Architecture diagrams: [HLD.md](HLD.md). Contract: [SPEC.md](../SPEC.md).
Evidence for every claim below is in [PLAN.md](../PLAN.md) (stage notes) and
[eval-results.md](eval-results.md).

## Where each requirement is answered

| Brief | Design | Prototype code | Verified by |
|---|---|---|---|
| R1 Hybrid intelligence | [R1](#r1-hybrid-intelligence--golden-bucket-format-sample-set-and-retrieval-implemented), HLD §5 | `golden/`, `src/retail_agent/golden/library.py` | `tests/unit/test_golden.py`, `tests/live/test_golden_live.py` |
| R2 Safety and PII | [R2](#r2-safety-and-pii-masking-implemented), ADR-0002, HLD §2 | `src/retail_agent/query/`, `agent/redaction.py`, `agent/grounding.py` | guard/scope/redaction tests, live scoping tests, red-team evals |
| R3 Destructive ops | [R3](#r3-high-stakes-oversight-implemented), HLD §4 | `reports/`, `delete_reports` in `agent/tools.py`, CLI confirmation | `test_reports.py`, `test_agent.py`, `test_cli.py`, red-team evals |
| R4 Learning loop | [R4](#r4-learning-loop-user-level-implemented-system-level-designed), HLD §5 | `agent/preferences.py`, `agent/definitions.py` | `test_preferences.py`, `test_definitions.py`, scenario evals |
| R5 Resilience | [R5](#r5-resilience-implemented) | `agent/resilience.py`, `agent/budget.py`, `agent/context.py`, `query/executor.py`, `cli/chat.py` | `test_resilience.py`, `test_context.py`, `test_executor.py`, `scripts/fault_injection.py` |
| R6 QA | [R6](#r6-quality-assurance-implemented-as-suites) | `tests/`, `evals/` | [eval-results.md](eval-results.md) |
| R7 Observability | [R7](#r7-observability-implemented) | `observability/`, `scripts/show_conversation.py` | `test_tracing.py`, `test_conversation.py` |
| R8 Persona | [R8](#r8-persona-management-implemented) | `agent/persona.py`, `agent/instruction.py`, `/persona` | `test_persona.py` |
| Extensibility | [Extending the agent](#extending-the-agent) | `agent/tools.py` | — |
| Setup and example run | [Setup and example run](#setup-and-example-run) | README | fresh-clone check (PLAN stage 11) |

## Principles

1. **The model proposes, deterministic code decides.** Gemini writes SQL and prose; whether a
   query may run, which rows it can see, whether a report may be deleted, and what may be
   shown are decided by code with tests, not by instructions to the model.
2. **One place per rule.** Brand scope lives in the scope binder, PII rules in the guard,
   confirmation in the delete tool. The instruction tells the model about the rules so it
   wastes fewer turns, but nothing relies on it obeying them.
3. **Measure, don't assume.** Every turn is traced, evals run against the real model and
   BigQuery, and expected numbers are computed at eval time rather than written down.

## Framework and model

- **Google ADK** ([ADR-0001](adr/0001-agent-framework.md)). Gemini-native, and it provides
  the pieces this brief needs: tool confirmation for human-in-the-loop deletes, callbacks
  for budgets and output redaction, plugins for tracing, session services for persistence,
  and an instruction that can be a function (persona without redeploy). PydanticAI would
  have been equally capable for tools and typing; ADK's built-in confirmation flow and
  plugin system decided it. **Experience:** over a year of production work with both Google
  ADK and PydanticAI.
- **Gemini 3.5 Flash Lite, fallback 3.1 Flash Lite** ([ADR-0003](adr/0003-model.md)). Both
  wrote valid, guard-passing SQL for 4/4 probe questions at about 1 s per call. The free tier
  showed the risks a production design must absorb: 35–50 s calls an hour earlier, 15
  requests per minute, and a model (2.5 Flash Lite) retired for new users during the
  project. All three are handled by the resilient model client below.

## R2 Safety and PII masking (implemented)

**Threat:** the model, a prompt injection or a curious user gets personal data, another
brand's numbers, or runs something destructive or expensive.

**Design — three layers, all deterministic:**

1. **Scope binding** ([ADR-0002](adr/0002-scoping-and-pii-in-the-query-layer.md)). The model
   writes SQL against four logical names. Before execution the server prepends CTEs with the
   same names that contain only the user's brands and the rows derived from them, without
   PII columns, with `user_id` replaced by a salted 16-hex `customer_key`. Brands and the
   salt are query parameters. The model cannot name a base table: fully qualified names are
   rejected. Customers and orders are visible only through in-scope purchases, and order
   totals are recomputed over in-scope items (net of cancelled and returned items).
2. **SQL guard** (sqlglot AST, BigQuery dialect). Single SELECT only; no DML/DDL/scripting,
   `EXPORT`, `CALL`, query parameters, `SESSION_USER`, `EXTERNAL_QUERY` or `ML.*`; only the
   four logical tables and the query's own CTEs; no PII column or raw `user_id`; `LIMIT`
   added or capped at 1,000. The **demographics rule** traces every output column through
   aliases, CTEs, subqueries and unions: if a result shows age, gender, city, state, country
   or traffic source, it must be an aggregate whose keys and outputs expose no customer,
   order or item id (including through `ARRAY_AGG`, `MIN`/`MAX`, value subqueries). In such
   a query an identifier may only be output or grouped in an inner step, counted, joined on
   another identifier or matched against an identifier subquery; comparing it with a value
   or passing it to a function (`=`, `BETWEEN`, `IN UNNEST`, `STARTS_WITH`, `REGEXP_CONTAINS`,
   `COUNTIF(customer_key = …)`) is rejected, and so is an identifier subquery cut by `LIMIT`
   below 5 rows. History: a bypass probe after the first green run found two holes, and an
   independent review later found seven more variants of "pinning" one customer — all are
   now tests (43 rejected and 18 accepted cases); every accepted case also runs on BigQuery
   in the live suite. Not caught: narrowing a group by value thresholds (`revenue > X`) to a
   handful of customers — that needs minimum group sizes (production).
3. **Output guard.** Tool results are capped (200 rows, 20k characters) and redacted; the
   final answer and saved reports are scanned for emails, phones, coordinates and street
   addresses. Traces store metadata and the redacted user message, never answers or rows.
4. **Grounded figures.** Every amount or count of 1,000 or more in the final answer (years
   excepted) must match a query result in the conversation — a value, a column total, or
   the sum or difference of two values, within rounding. Otherwise the answer carries an
   "unverified figures" note and the trace records them. Added after eval run 1 caught the
   model stating a $144M revenue figure without running any query — the instruction said
   "numbers only from run_query", which is exactly why it is now checked in code.

**Cost guard:** every query is dry-run first; above 1 GB it is rejected with advice to
narrow it, and the real run carries `maximum_bytes_billed`. Typical queries scan 2–13 MB.

**Production additions:** authorized views with row-level security as a second wall behind
the binder; column-level policy tags; BigQuery aggregation threshold rules for a minimum
group size (the prototype does not suppress small groups — a filter such as
"top customers in California" implies the state of every row, a documented limit);
per-user byte quotas.

## R3 High-stakes oversight (implemented)

**Design:** deleting reports is two-phase and the second phase cannot come from the model.

- The tool resolves the selector (ids, mentions of an entity such as "Client X", this
  conversation, older than a date) to the user's own reports only; others' reports are
  never matched or mentioned.
- No match → it says so. More than 20 → it asks the user to narrow the request.
- Otherwise it stores the exact ids in the session, writes an audit entry and calls ADK's
  `request_confirmation`; the model is told explicitly that nothing has been deleted yet
  (it is called again right after the pause, so this matters).
- The CLI shows the list and asks for `delete N`. The answer travels as a function response
  only a client can send. The tool checks the phrase itself and **hard-deletes exactly the
  previewed ids** that still exist and belong to the user (the client asked for hard
  delete), then audits the outcome. The audit log has ids and actions, never content.

**Reading reports:** `get_report(id)` and `/report <id>` open one of the user's own reports
so it can be discussed; other users' ids return "not found".

**Extensibility:** any side-effecting tool (send email, share externally) uses the same
pattern: plan → preview → client confirmation → execute exactly the plan.

## R5 Resilience (implemented)

- **Model:** `ResilientLlm` wraps primary and fallback. 429 → switch model at once (separate
  quota); both limited → one wait for the advertised `RetryInfo` delay if ≤ 30 s; timeout
  (25 s) or 404 → switch; 5xx/network → one retry with backoff and jitter; circuit breaker
  per model (3 failures → skip for 60 s); other 4xx raised. Reproducible with `scripts/fault_injection.py`: a retired primary (404) falls back to the
  other model, a 0.01 s timeout on both ends in a friendly message, no crash. One run is
  worth noting: on the same question the primary answered 275 orders and the fallback 214 —
  a fallback can change answers, so the eval suite should also be run with the fallback as
  the primary before relying on it.
- **SQL self-correction:** guard rejections and BigQuery errors return to the model with the
  reason and how to fix it (e.g. BigQuery does not escape quotes by doubling them — a real
  failure seen in traces). At most 3 failed attempts and 8 queries per turn, 12 model calls
  per turn; when a budget is hit the agent answers with what it has.
- **BigQuery:** transient errors retried with backoff; permission, quota and invalid-query
  errors classified so the model knows whether rewriting can help.
- **CLI:** no exception escapes a turn; the user sees one line with the trace id, details go
  to `logs/cli.log`; sessions persist and resume after a restart.
- **Long conversations:** before every model call, tool results of earlier turns are
  compacted to their metadata (`agent/context.py`); the answers written from them stay, so
  context and cost stay bounded. A context-too-long error tells the user to start `/new`.
- **One deletion at a time:** a second delete request while one awaits confirmation is
  refused with a message instead of being silently dropped.

## R7 Observability (implemented)

- **Traces:** an ADK plugin writes one span per step — user message (redacted), model call
  (model, attempts incl. fallback, tokens, latency, finish reason), tool call (SQL, guard
  rule, rows, bytes, latency), confirmation, and a turn summary — keyed by the invocation id
  shown in the CLI footer. `/trace` renders the timeline.
- **Metrics** (`/stats`, `python -m retail_agent.observability.metrics`): turn success rate,
  SQL first-try success, self-correction rate, empty results, guard rejections by rule,
  PII redactions, model fallbacks and attempt outcomes, tokens, BigQuery bytes per turn,
  latency p50/p95 per turn, model call and tool call, delete confirmations, user feedback
  (`/feedback good|bad`).
- **It already paid off:** a trace showed the model adding a redundant brand filter written
  as `'Levi''s'`, costing a rejected query per question; one instruction line removed it.
- **Deep dive — the message correspondence:** traces hold metadata and the redacted user
  message, not answers or result rows. The
  conversation itself (user and model messages, every tool call with its arguments and
  result, confirmations) is in the ADK session store, and
  `uv run python scripts/show_conversation.py <trace_id>` prints the turn behind any trace
  id from the footer or `logs/traces.jsonl`. A typical investigation: an alert on SQL
  first-try success → `/stats` or the metrics module to find the turns → the trace timeline
  (`/trace`) to find the failing step → the correspondence to see the SQL, the guard message
  and what the model did next.
- **Production:** ADK emits OpenTelemetry spans natively → Cloud Trace; the JSON spans go to
  Cloud Logging with a BigQuery sink; dashboards on the metrics above; alerts on success
  rate, fallback rate, p95 latency, bytes per turn, unverified figures and any PII
  redaction. Sessions live in Cloud SQL; reading them is limited to a support role and kept
  for a fixed retention period.

## R1 Hybrid intelligence — golden bucket (format, sample set and retrieval implemented)

- **Format:** one YAML file per trio: question, intent, SQL (logical names), report
  (structure and kind of insight — no figures), definitions used, tags, brands, author,
  status (`seed`/`reviewed`/`retired`), reviewed date. 25 seed trios cover every question
  type in the brief, including its two "why" examples (state underspending, a churn spike);
  each passes the guard and returns rows on BigQuery (live tests).
- **Scope safety:** trios about specific brands are returned only to users whose scope covers
  them, and reports carry no figures (tested), so an example can never leak another brand's
  numbers.
- **Retrieval:** `find_similar_analyses` embeds the question with `gemini-embedding-001`
  (256 dims) and ranks precomputed trio embeddings (`golden/index.json`, built by a script,
  validated by a hash per trio). Only matches above a calibrated similarity are returned
  (cosine 0.70: off-topic questions scored at most 0.671, the weakest on-topic match 0.698);
  otherwise the tool says there is no close example. A stale index is logged and local
  TF-IDF is used instead; so is an embedding failure. The brief's two example questions
  retrieve their trios first (0.768, 0.86); 5/5 paraphrases retrieve the right trio live.
  It is a tool the instruction tells the model to call first for a new question, and the
  scenario evals check that it was called.
- **Production:** trios in a versioned GCS bucket; Vertex AI Vector Search with metadata
  filters (brands, tags, status); hybrid vector + keyword ranking; a nightly job re-runs each
  trio's SQL and retires failing ones; new trios come from the learning loop below.

## R4 Learning loop (user level implemented, system level designed)

- **User level — how answers look (implemented):** `remember_preference` saves, per user,
  `format` (tables, bullets, prose), `depth` (brief, standard, detailed) and `visuals` (text,
  charts) when the user states them ("I prefer bullet points, keep it brief"). Every later
  turn and conversation gets them in the instruction; `/preferences` shows them. Live: after
  Bob said so, a new conversation answered the top-categories question in three short
  bullets instead of a table; Alice's answers were unaffected.
- **User level — what words mean (implemented):** business definitions have a default, a
  conversation override, and a saved per-user value set only on an explicit "remember /
  always"; every answer names the definition and its source.
- **User level — learning without being told (designed):** signals already in the traces —
  "/feedback bad: too long", follow-ups like "give me just the numbers", a user who always
  asks to "break it down" — feed a nightly job that proposes a preference; the agent asks
  once ("Shall I keep answers brief from now on?") and saves it only on yes, so
  preferences stay explainable and reversible.
- **System level:** traces and feedback are mined nightly for failed or corrected queries,
  empty results and bad ratings; the cases become candidate trios with the final working
  SQL; analysts review them; approved trios are indexed and added to the regression evals.
  This keeps the improvement loop reviewable and avoids fine-tuning.

## R6 Quality assurance (implemented as suites)

- **Offline, every change (`make test`):** guard, scope binding, executor, deletion flow,
  resilience, history compaction, tracing, golden format, definitions, preferences,
  persona, CLI — 378 tests, a
  scripted model driving the real ADK runner.
- **Live (`make test-live`):** scoping on real data, every query the guard accepts, every
  golden trio, embedding retrieval.
- **Evals (`make eval`):** scenarios with reference SQL computed at eval time (numbers and
  names must appear in the answer, definitions stated, tools used) and a red-team suite
  with deterministic checks (no PII, no out-of-scope numbers computed by an oracle with full
  access, no customer's real location, reports still present after unconfirmed deletes, only
  SELECTs executed, no code for off-topic requests). Results: [eval-results.md](eval-results.md).
- **Does a report answer the user's intent? (designed):** three layers, cheapest first.
  (1) *Facts:* reference SQL per golden question, numbers must match (implemented in the
  scenario evals) and every figure must be grounded in a query result (implemented in the
  output guard). (2) *Coverage:* each golden question carries a short intent checklist
  ("names the top category", "compares with the previous quarter", "gives at least two
  action items with an owner area"); an LLM judge marks each item present or absent with
  the quoted sentence as evidence — binary, quotable checks rather than a 1–10 score.
  (3) *Calibration:* analysts rate a monthly sample; the judge is used only while it agrees
  with them (e.g. Cohen's kappa ≥ 0.7), and disagreements become new checklist items.
- **UX (designed, signals already traced):** task success (the turn ends with an answer, not
  a budget stop or error), turns to answer and the rephrase rate (the same question asked
  again within two turns), feedback rate and share of bad ratings, latency p95, unverified
  figures per 100 answers, refusals that users then contest. Pre-launch: moderated sessions
  with 5–8 executives on scripted tasks; post-launch: these metrics per release.
- **Release gate:** offline and live suites green; scenario pass rate and red-team 100% at
  or above the previous release; no new PII redactions or unverified figures in the eval
  traces; any prompt, persona or model change runs the same gate.

## R8 Persona management (implemented)

The instruction is a function evaluated on every turn, so tone changes apply from the next
answer with no restart or redeploy. A **persona editor** (the CEO in `config/users.yaml`)
changes it from the chat itself: `/persona` shows the current text, `/persona set <text>`
replaces it, `/persona history` lists replaced versions with who replaced them. The change
is written to `data/persona.md` (runtime state; `config/persona.md` stays the shipped
default) and the old text to `data/persona_history/`. Other users get "only a persona
editor can change the persona". Rules, scope, schema, definitions and preferences are
added by code — the rules first, the persona labelled as tone only — so a persona cannot
remove a safety rule. Live: the CEO
set "upbeat sports commentator" and the next answer opened with "Boom! What a year!" while
the figure stayed exact.

Production: the same contract behind a small admin UI over a versioned config store
(Firestore or Cloud SQL): edit, preview against the eval suite, publish, roll back; the
persona version id goes into every trace so quality changes can be tied to it.

## Cost and scale

- **BigQuery:** scoped CTEs read the base tables on every query (2–13 MB here; well inside
  the 1 TB/month free tier). At scale: authorized views, partitioned and clustered
  materialized aggregates for frequent questions, BI Engine, per-user byte quotas.
- **Model:** in the stage 7 session a turn took 2–4 model calls; 6 calls used 16,885 input
  and 1,249 output tokens (about 2.8k input tokens per call, mostly the instruction and
  schema). Context caching of the static instruction prefix and a cheaper model for simple
  turns are the next levers.
- **Runtime:** stateless agent service on Cloud Run; sessions, reports and preferences in
  Cloud SQL; scales horizontally.

## Extending the agent

New capabilities (charts, sending reports by email, web search for trends) are tools.
Adding one is local and follows the same path as the existing tools:

1. Write a function in `agent/tools.py` (or a module it imports) that takes its inputs as
   arguments and reads identity only from `tool_context.session.user_id`; register it in
   the list `build_tools` returns. ADK derives the schema from the signature and docstring.
2. Read-only tools that touch data go through the query layer (`execute`), so scope, PII
   rules, cost caps and budgets apply automatically.
3. Tools with side effects outside the system (send an email, share a report) use the
   delete pattern: plan → `request_confirmation` with the exact preview (recipients,
   attachment) → execute exactly the plan → audit entry.
4. Tracing, metrics, budgets and the output guard need no changes: the plugin and callbacks
   wrap every tool call.
5. Add unit tests with the scripted model, golden trios for the new kind of question, and
   eval cases (including a red-team case for abuse of the new tool).

Examples: a chart tool returns a PNG built from a `run_query` result (matplotlib/Vega-Lite)
and the CLI saves it; `email_report(report_id, to)` confirms recipients first; a web-search
tool returns cited snippets that the instruction marks as external, unverified context.

## Setup and example run

Full instructions are in the [README](../README.md#run-it); in short: install uv, `make
setup` (or `pip install -r requirements.txt -r requirements-dev.txt && pip install -e .`),
`gcloud auth application-default login` with any project that has the BigQuery API, copy
`.env.example` to `.env` with a Gemini key, the project id and a random salt, then
`make run USER_ID=bob`. The README shows a real session excerpt. The procedure was checked
on a fresh clone (PLAN stage 11).

## Limitations and next steps

- Small-group suppression is design-only (aggregation thresholds in production).
- The golden trios are seeds written for the prototype, not reviewed by an analyst.
- The free tier limits eval size and can make turns slow; the paid key changes only `.env`.
- Charts, email and web trends are extension points ([Extending the agent](#extending-the-agent)),
  not implemented; a `visuals: charts` preference yields chart-ready tables.
- Preferences are learned from explicit statements only; inferring them is designed.
