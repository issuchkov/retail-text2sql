# High-level design — Retail Data Analysis Chat Agent

Scope and behaviour are specified in [SPEC.md](../SPEC.md); the reasoning behind each
choice is in [technical-explanation.md](technical-explanation.md) and the
[ADRs](adr/). This document is the target architecture; the last section maps each
component to what the prototype in this repository implements.

## 1. Architecture

```mermaid
flowchart TB
    subgraph Clients
        WEB[Web chat UI]
        CLI[CLI prototype]
    end

    subgraph Edge
        GW[API gateway<br/>JWT verification<br/>rate limits]
    end

    subgraph Agent["Agent service (Cloud Run, Google ADK)"]
        RUN[ADK Runner<br/>sessions + plugins]
        LLM[LlmAgent<br/>instruction rebuilt per turn]
        RES[Resilient model client<br/>primary / fallback, breaker]
        BUD[Per-turn budgets]
        OUT[Output guard<br/>PII redaction<br/>grounded figures]
        subgraph Tools
            Q[run_query<br/>list_column_values<br/>describe_data]
            G[find_similar_analyses]
            R[save / list / delete reports]
            D[definitions and<br/>answer preferences]
            X[future tools<br/>charts, email, web search]
        end
        subgraph QueryLayer["Query layer (deterministic)"]
            GUARD[SQL guard<br/>sqlglot AST rules]
            SCOPE[Scope binder<br/>brand-scoped, PII-free CTEs]
            EXE[Executor<br/>dry run, byte cap, retries]
        end
    end

    subgraph Data
        BQ[(BigQuery<br/>thelook_ecommerce)]
        VIEWS[(Authorized views<br/>row-level security<br/>aggregation thresholds)]
        SQLDB[(Cloud SQL<br/>reports, audit log,<br/>user preferences)]
        GCS[(GCS golden bucket<br/>trio files, versioned)]
        VEC[(Vector index<br/>Vertex AI Vector Search)]
        CFG[(Config store<br/>persona, prompts, versions)]
    end

    subgraph Models
        GEM[Gemini Flash Lite<br/>primary]
        GEM2[Gemini Flash Lite<br/>fallback]
        EMB[Gemini embeddings]
    end

    subgraph Ops["Observability and quality"]
        OTEL[OpenTelemetry spans<br/>Cloud Trace]
        LOG[Cloud Logging<br/>BigQuery sink]
        DASH[Dashboards and alerts]
        EVAL[Eval pipeline<br/>scenarios + red team]
        REVIEW[Analyst review queue]
    end

    WEB --> GW --> RUN
    CLI --> RUN
    RUN --> LLM --> RES --> GEM & GEM2
    LLM --> BUD
    LLM --> OUT
    LLM --> Tools
    Q --> GUARD --> SCOPE --> EXE --> VIEWS --> BQ
    G --> VEC
    G --> EMB
    GCS --> VEC
    R --> SQLDB
    D --> SQLDB
    X -. confirmation for side effects .-> R
    LLM -. persona .-> CFG
    RUN -. spans .-> OTEL
    RUN -. spans .-> LOG --> DASH
    LOG --> EVAL
    LOG --> REVIEW --> GCS
```

The model never touches data directly. Every number reaches it through `run_query`, and
every query passes the same deterministic layer: the guard rejects anything that is not a
single read-only query over the four logical tables, the scope binder rewrites the table
names into brand-scoped, PII-free views of the data, and the executor dry-runs the query
under a byte cap before running it. Identity and scope come from the verified JWT via the
session, never from model arguments.

### Services and how they talk

| Building block | Service | Talks to | How |
|---|---|---|---|
| Web UI / CLI | Cloud Run (web), local CLI | Agent service | HTTPS + SSE streaming; JWT in `Authorization` |
| API gateway | API Gateway or Cloud Run ingress + IAP | Agent service | Verifies the JWT (issuer, audience, expiry, signature), rate-limits per user |
| Agent service | Cloud Run, Python, Google ADK | Gemini, BigQuery, Cloud SQL, Vector Search, config store | HTTPS/gRPC client libraries with a service account; no user credentials reach it |
| Models | Gemini via Vertex AI (paid) or Gemini API | — | Primary and fallback; embeddings for retrieval |
| Analytics data | BigQuery (`thelook_ecommerce` through authorized views) | — | Jobs run as the service account, dry run + byte cap per query |
| Application data | Cloud SQL for PostgreSQL | — | Sessions, reports, audit, preferences, definitions |
| Golden bucket | Cloud Storage + Vertex AI Vector Search | — | Trio files versioned in GCS, embeddings and metadata in the index |
| Config | Firestore (persona versions) | — | Read on every turn, cached briefly |
| Observability | Cloud Trace, Cloud Logging → BigQuery, Cloud Monitoring | — | OpenTelemetry spans from ADK; JSON spans as logs |

## 2. Data: where it lives and how it is handled

| Data | Contains | Prototype | Production | PII | Retention and access |
|---|---|---|---|---|---|
| Transaction logs | orders, items, products, customers | BigQuery public dataset, read through scoped CTEs | Authorized views with row-level security and policy tags | Names, email, address, postal code, coordinates exist only here and are never selected | Read-only; the service account cannot write |
| Query results | rows for one tool call | In memory; returned to the model capped (200 rows, 20k chars) and redacted | Same | None (PII columns are not selectable; customers are `customer_key` pseudonyms) | Kept only inside the session (below) |
| Sessions | messages, tool calls and results, state | SQLite (`data/sessions.sqlite`, ADK `DatabaseSessionService`) | Cloud SQL | Pseudonymous only | Owner and support role; retention e.g. 90 days |
| Saved reports | title, Markdown body, entities, owner | SQLite (`data/reports.sqlite`) | Cloud SQL with owner predicates and row-level security | Redacted before saving | Author only; hard delete on confirmation; backups per retention policy |
| Audit log | who requested, previewed, confirmed, deleted which ids | SQLite table | Cloud Logging (write-once) | None (ids and actions, no content) | Compliance retention |
| Preferences and definitions | format, depth, visuals; business terms | SQLite (`data/preferences.sqlite`) | Cloud SQL | None | Per user; the user can reset |
| Persona | tone text and versions | `config/persona.md` default, `data/persona.md` + history | Firestore with versions | None | Persona editors write, everyone reads |
| Golden trios | question, SQL, report outline | `golden/trios/*.yaml`, `golden/index.json` | GCS (versioned) + Vector Search | No figures, no PII (tested) | Analysts curate; scope-filtered on read |
| Traces | spans: steps, models, SQL, rows, bytes, latency | `logs/traces.jsonl` | Cloud Trace + Logging → BigQuery | Metadata only, every string redacted | Engineering; retention e.g. 30 days |
| Secrets | Gemini key, salt, credentials | `.env` (git-ignored), ADC | Secret Manager, service accounts | — | Never in the repo, logs or prompts |

## 3. A question, end to end

```mermaid
sequenceDiagram
    actor U as Executive
    participant A as Agent (ADK)
    participant M as Gemini
    participant G as Golden library
    participant Q as Query layer
    participant B as BigQuery

    U->>A: "Why does Levi's sell differently from Wrangler?"
    A->>A: instruction = persona + scope + schema + definitions (per turn)
    A->>M: question + instruction + tools
    M->>A: call find_similar_analyses
    A->>G: embed question, top 3 trios in the user's scope
    G-->>A: examples (SQL patterns, no figures)
    A->>M: examples
    M->>A: call run_query(sql)
    A->>Q: guard → bind scope → dry run (byte cap)
    Q->>B: scoped query, maximum_bytes_billed
    B-->>Q: rows
    Q-->>A: rows (capped, redacted)
    A->>M: rows as data
    M-->>A: answer + definitions used
    A->>A: output guard (PII patterns), trace span
    A-->>U: answer · trace id · queries · MB
```

Failures stay inside the turn. A rejected or failing query goes back to the model with an
actionable message (at most 3 failed attempts per turn). A rate-limited, slow or retired
model is replaced by the fallback. When a budget is reached, the agent answers with what it
has.

## 4. Destructive operations

```mermaid
sequenceDiagram
    actor U as Executive
    participant A as Agent
    participant T as delete_reports tool
    participant S as Report store

    U->>A: "Delete all reports that mention Client X"
    A->>T: delete_reports(mentions="Client X")
    T->>S: resolve selector — owner's reports only
    S-->>T: 2 reports
    T->>S: audit: delete_preview [ids]
    T-->>A: request confirmation (ids, titles, dates) — nothing deleted
    A-->>U: exact list + "type delete 2"
    U->>A: "delete 2" (UI confirmation response, not chat text)
    A->>T: resume with confirmation
    T->>T: phrase matches the previewed count?
    T->>S: hard delete exactly the previewed ids (still owned)
    T->>S: audit: delete_confirmed [ids]
    T-->>A: deleted [ids]
    A-->>U: "Deleted 2 reports"
```

The confirmation is ADK's tool-confirmation flow: the model cannot produce it, only the
client can, and the tool checks the typed phrase itself. More than 20 matches → no preview;
the user narrows the request. Other users' reports are never matched or mentioned.

## 5. Learning loop and golden knowledge

```mermaid
flowchart LR
    T[Traces + feedback<br/>good / bad, corrections] --> MINE[Nightly mining<br/>failed queries, self-corrections,<br/>low ratings, saved reports]
    MINE --> CAND[Candidate trios<br/>question, SQL, report outline]
    CAND --> REV[Analyst review<br/>edit, approve, reject]
    REV -->|approved| GCS[(Golden bucket<br/>GCS, versioned)]
    GCS --> IDX[Re-index<br/>embeddings + metadata]
    IDX --> VEC[(Vector index)]
    VEC --> RET[find_similar_analyses<br/>scope-filtered top k]
    GCS --> REG[Regression evals<br/>trio SQL still runs,<br/>retrieval still finds it]
    U[User statements<br/>always use 60 days for churn] --> PREF[(User preferences<br/>definitions, format)]
    PREF --> INS[Instruction per turn]
```

- **User level:** explicit preferences ("always use 60 days for churn", "tables, not
  bullets") are stored per user and injected into every turn; one-off overrides live in the
  conversation only.
- **System level:** traces show where the agent struggles (rejected SQL, empty results,
  self-corrections, bad ratings). Those cases become candidate trios that an analyst
  reviews. Approved trios are versioned in the golden bucket, re-indexed, and added to the
  regression evals, so the system improves without fine-tuning and every change can be
  reviewed.

## 6. Prototype vs production

| Component | Prototype (this repo) | Production |
|---|---|---|
| Client and identity | CLI, `--user` over `config/users.yaml` | Web UI; JWT with `sub` and brand scopes, verified at the gateway |
| Agent runtime | Google ADK, local process | Same code on Cloud Run, horizontal scaling |
| Sessions | ADK `DatabaseSessionService` on SQLite | ADK `DatabaseSessionService` on Cloud SQL, or `VertexAiSessionService` |
| Brand scope | Scoped CTEs built per query, values as parameters | Same binder over authorized views with row-level security as a second wall |
| PII | Columns never selected; pseudonymous `customer_key`; demographics-in-aggregates rule in the guard; output redaction | Same, plus BigQuery aggregation threshold rules (minimum group size) and column-level policy tags |
| Query cost | Dry run, `maximum_bytes_billed` 1 GB, 1,000-row cap | Same, plus per-user daily byte quotas and BI Engine or materialized aggregates for hot queries |
| Reports and audit | SQLite, hard delete, audit table | Cloud SQL with owner predicates and row-level security, audit log to Cloud Logging, backups with retention |
| Golden bucket | 23 seed YAML trios, local embedding index, TF-IDF fallback | GCS bucket with versioned trios, Vertex AI Vector Search with metadata filters, review queue |
| Preferences and definitions | SQLite per user; `remember_preference`, `set_definition`, `/preferences`, `/definitions` | Cloud SQL; inferred preferences proposed to the user |
| Persona and prompts | Re-read every turn; persona editors use `/persona set` in the chat, versions kept | Versioned config store with edit UI, eval preview, staged rollout and rollback |
| Deep-dive debugging | `scripts/show_conversation.py <trace_id>` over the session store | Support console over Cloud SQL sessions joined with Cloud Trace |
| Extensions | Tool registry in `agent/tools.py` | Same; charts, email and web search as new tools behind confirmation and tracing |
| Model | Gemini 3.5 Flash Lite + 3.1 Flash Lite fallback, free tier | Paid tier or Vertex AI with provisioned throughput; same fallback logic |
| Observability | JSON-line spans, `/trace`, `/stats` | ADK OpenTelemetry → Cloud Trace; logs → BigQuery sink → dashboards and alerts |
| QA | Offline tests, live tests, scenario + red-team evals | Same suites in CI and nightly, results tracked over time |
