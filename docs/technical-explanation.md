# Technical explanation

How each requirement is met, which technology was chosen and why, and where the prototype
stops. Architecture diagrams: [HLD.md](HLD.md). Contract: [SPEC.md](../SPEC.md).
Evidence for every claim below is in [PLAN.md](../PLAN.md) (stage notes) and
[eval-results.md](eval-results.md).

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
  plugin system decided it.
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
   order or item id (including through `ARRAY_AGG`, `MIN`/`MAX`, value subqueries), and no
   filter may pin an identifier to literal values (`WHERE customer_key = 'abc'`). A bypass
   probe after the first green run found two holes; both are now tests. 29 rejected and 17
   accepted cases; every accepted case also runs on BigQuery in the live suite.
3. **Output guard.** Tool results are capped (200 rows, 20k characters) and redacted; the
   final answer and saved reports are scanned for emails, phones, coordinates and street
   addresses. Traces store metadata only and are redacted too.
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

**Extensibility:** any side-effecting tool (send email, share externally) uses the same
pattern: plan → preview → client confirmation → execute exactly the plan.

## R5 Resilience (implemented)

- **Model:** `ResilientLlm` wraps primary and fallback. 429 → switch model at once (separate
  quota); both limited → one wait for the advertised `RetryInfo` delay if ≤ 30 s; timeout
  (25 s) or 404 → switch; 5xx/network → one retry with backoff and jitter; circuit breaker
  per model (3 failures → skip for 60 s); other 4xx raised. Verified live by pointing the
  primary at a nonexistent model (fallback answered the same number) and by a 0.01 s timeout
  (friendly message, no crash).
- **SQL self-correction:** guard rejections and BigQuery errors return to the model with the
  reason and how to fix it (e.g. BigQuery does not escape quotes by doubling them — a real
  failure seen in traces). At most 3 failed attempts and 8 queries per turn, 12 model calls
  per turn; when a budget is hit the agent answers with what it has.
- **BigQuery:** transient errors retried with backoff; permission, quota and invalid-query
  errors classified so the model knows whether rewriting can help.
- **CLI:** no exception escapes a turn; the user sees one line with the trace id, details go
  to `logs/cli.log`; sessions persist and resume after a restart.

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
- **Production:** ADK emits OpenTelemetry spans natively → Cloud Trace; the JSON spans go to
  Cloud Logging with a BigQuery sink; dashboards on the metrics above; alerts on success
  rate, fallback rate, p95 latency, bytes per turn and any PII redaction.

## R1 Hybrid intelligence — golden bucket (format, sample set and retrieval implemented)

- **Format:** one YAML file per trio: question, intent, SQL (logical names), report
  (structure and kind of insight — no figures), definitions used, tags, brands, author,
  status (`seed`/`reviewed`), reviewed date. 23 seed trios cover every question type in the
  brief; each passes the guard and returns rows on BigQuery (live tests).
- **Scope safety:** trios about specific brands are returned only to users whose scope covers
  them, and reports carry no figures (tested), so an example can never leak another brand's
  numbers.
- **Retrieval:** `find_similar_analyses` embeds the question with `gemini-embedding-001`
  (256 dims) and ranks precomputed trio embeddings (`golden/index.json`, built by a script,
  validated by a hash per trio); on a stale index or an embedding failure it falls back to
  local TF-IDF. 5/5 paraphrased questions retrieved the right trio live. It is a tool rather
  than automatic context: visible in traces, free on follow-up questions.
- **Production:** trios in a versioned GCS bucket; Vertex AI Vector Search with metadata
  filters (brands, tags, status); hybrid vector + keyword ranking; a nightly job re-runs each
  trio's SQL and retires failing ones; new trios come from the learning loop below.

## R4 Learning loop (user level implemented, system level designed)

- **User level:** business definitions have a default, a conversation override, and a saved
  per-user value set only on an explicit "remember / always". The instruction lists every
  definition with its source and the answer states it ("churn: 60 days (your saved
  definition)"). The same store would hold format preferences.
- **System level:** traces and feedback are mined nightly for failed or corrected queries,
  empty results and bad ratings; the cases become candidate trios with the final working
  SQL; analysts review them; approved trios are indexed and added to the regression evals.
  This keeps the improvement loop reviewable and avoids fine-tuning.

## R6 Quality assurance (implemented as suites)

- **Offline, every change (`make test`):** guard, scope binding, executor, deletion flow,
  resilience, tracing, golden format, definitions, CLI — over 300 tests, a scripted model
  driving the real ADK runner.
- **Live (`make test-live`):** scoping on real data, every query the guard accepts, every
  golden trio, embedding retrieval.
- **Evals (`make eval`):** scenarios with reference SQL computed at eval time (numbers and
  names must appear in the answer, definitions stated, tools used) and a red-team suite
  with deterministic checks (no PII, no out-of-scope numbers computed by an oracle with full
  access, no customer's real location, reports still present after unconfirmed deletes, only
  SELECTs executed, no code for off-topic requests). Results: [eval-results.md](eval-results.md).
- **Production additions:** an LLM-as-judge rubric for report quality (structure, insight,
  actionability) calibrated against analyst ratings; UX metrics (task success, turns to
  answer, feedback rate) from traces; evals in CI on every prompt or model change.

## R8 Persona management (implemented lite)

The instruction is a function evaluated on every turn; it reads `config/persona.md`, so tone
and wording change without a restart (tested). Rules, scope, schema and definitions are
appended by code so a persona edit cannot remove a safety rule. Production: a versioned
config store with an edit UI, preview against the eval suite, staged rollout and rollback;
the persona version is recorded in each trace.

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

## Limitations and next steps

- Small-group suppression is design-only (aggregation thresholds in production).
- The golden trios are seeds written for the prototype, not reviewed by an analyst.
- The free tier limits eval size and can make turns slow; the paid key changes only `.env`.
- Charts, email and web trends are extension points (tools behind the same guard,
  confirmation and tracing), not implemented.
