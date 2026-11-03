# Retail data analysis chat agent

A chat agent for retail executives: ask questions about sales, products and customers in
plain language, get answers computed by SQL on BigQuery
(`bigquery-public-data.thelook_ecommerce`), turn them into saved reports, and manage the
report library — without ever seeing personal data or another brand's numbers.

Built with **Google ADK** and **Gemini** (why: [ADR-0001](docs/adr/0001-agent-framework.md);
the author has over a year of production experience with Google ADK and PydanticAI). The
model writes SQL; deterministic code decides what may run, what it can see and what may be
shown.

| Document | What it covers |
|---|---|
| [docs/HLD.md](docs/HLD.md) | Architecture (Mermaid), request and confirmation flows, learning loop, prototype vs production |
| [docs/technical-explanation.md](docs/technical-explanation.md) | Every requirement: design, technology choice, reasoning, limits |
| [SPEC.md](SPEC.md) | Behaviour contract and acceptance criteria, incl. the client's answers |
| [PLAN.md](PLAN.md) | Stages, evidence and decisions log |
| [docs/eval-results.md](docs/eval-results.md) | Scenario and red-team eval results |
| [docs/adr/](docs/adr/) | Framework, scoping/PII, model choice |

## What it does

- **Answers analysis questions** — customer behaviour, product comparisons ("why does
  Levi's perform differently from Wrangler?"), time series, multi-step questions, questions
  about the data itself; follow-ups in the same conversation; reports with findings, action
  items and the SQL used.
- **Brand scope** — each manager sees only their brands (the CEO sees all), enforced by
  rewriting every query server-side, not by the prompt.
- **No PII** — names, emails, addresses, postal codes and coordinates never reach the model;
  customers appear as a salted pseudonym; demographics only in aggregates (checked on the SQL
  itself, including attempts to pin a "group" to one customer).
- **Safe deletes** — "delete all reports that mention Client X" shows the exact list and
  deletes only after the user types `delete N`; only the author's reports are ever matched.
- **Resilience** — fallback model on rate limits, timeouts and retired models; SQL
  self-correction with a budget; the chat never crashes.
- **Observability** — a trace per turn (`/trace`), session metrics (`/stats`), feedback.
- **Golden knowledge** — analysts' Question → SQL → Report examples retrieved by embedding
  similarity, filtered by the user's scope.
- **Learns each user's preferences** — "I prefer bullet points, keep it brief" is saved per
  user and shapes every later answer; business definitions likewise ("use 60 days for churn"
  for one conversation, "always use 60 days" saved).
- **Persona without redeploy** — the CEO changes the agent's tone from the chat with
  `/persona set ...`; it applies from the next answer, with version history.

## Run it

Prerequisites: [uv](https://docs.astral.sh/uv/) (installs Python 3.12),
the [gcloud CLI](https://cloud.google.com/sdk/docs/install), a Google Cloud project with the
BigQuery API enabled (the free BigQuery Sandbox is enough — the dataset is public, queries run
in your project), and a [Gemini API key](https://aistudio.google.com/apikey) (free tier works).

```bash
git clone https://github.com/issuchkov/retail-text2sql.git
```

```bash
cd retail-text2sql && make setup
```

Without uv, plain pip works too (Python 3.12):

```bash
pip install -r requirements.txt -r requirements-dev.txt && pip install -e .
```

Authenticate BigQuery with Application Default Credentials and set the project that runs the
queries:

```bash
gcloud auth application-default login
```

```bash
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Create `.env` (git-ignored) from the template and fill in `GOOGLE_API_KEY`,
`GOOGLE_CLOUD_PROJECT` and `RETAIL_PSEUDONYM_SALT`:

```bash
cp .env.example .env
```

```bash
openssl rand -hex 16
```

Chat as one of the configured users (`config/users.yaml`):

```bash
make run USER_ID=bob
```

| User | Role | Scope |
|---|---|---|
| `alice` | Brand Manager, Calvin Klein | Calvin Klein |
| `bob` | Denim Portfolio Manager | Levi's, Wrangler, Diesel |
| `carol` | Chief Executive Officer, persona editor | all brands |

`retail-agent chat --user bob --new` starts a new conversation; without `--new` the last one
resumes.

### Example

Excerpts of a real session as `bob` (2026-11-03; the dataset is refreshed daily, so numbers
move):

```text
you> Which of my brands had the highest net revenue in the last full quarter?
Diesel generated the highest net revenue in the last full quarter (Q3 2026, July 1 –
September 30, 2026) with $20,508.87, significantly leading your portfolio.

 Brand     Net Revenue (Q3 2026)
 Diesel    $20,508.87
 Levi's    $8,385.46
 Wrangler  $5,162.09

Definitions used:
 • Last full quarter: Q3 2026 (July 1, 2026 – September 30, 2026 UTC).
 • Net revenue: Sum of sale_price of items whose status is not Cancelled or Returned.
trace e-ff58bc7d-40d7-4062-a18d-c67e38dee589 · 1 query · 7 MB

you> Delete my Q3 2026 brand review report.
I have found 1 report matching "Q3 2026 brand review" ...
Please confirm that you would like to permanently delete this report.

      Reports to delete permanently
┃ id ┃ title                ┃ created    ┃
│ 1  │ Q3 2026 brand review │ 2026-11-02 │
Type delete 1 to delete, anything else cancels: delete 1
The report "Q3 2026 brand review" has been successfully deleted from your library.
```

### Commands

| Command | |
|---|---|
| `/sql` | SQL behind the last answer (scope is applied server-side) |
| `/trace` | Steps of the last answer: model calls, tools, tokens, timings |
| `/stats` | Metrics for this conversation |
| `/feedback good\|bad [note]` | Rate the last answer |
| `/definitions` | Business definitions in effect and their source |
| `/preferences` | Your saved answer preferences (format, depth, charts) |
| `/persona` | The agent's tone; persona editors: `/persona set <text>`, `/persona history` |
| `/reports` | Your saved reports |
| `/new`, `/help`, `/quit` | |

## Tests and evals

```bash
make test
```

Offline (no network): guard, scope binding, executor, deletion flow, resilience, tracing,
golden set, definitions, preferences, persona, CLI — a scripted model drives the real ADK
runner.

```bash
make test-live
```

BigQuery: scoping on real data, every query the guard accepts, every golden trio, embedding
retrieval.

```bash
make eval
```

Real Gemini + BigQuery: 15 scenarios with reference SQL computed at eval time and 15
red-team cases with deterministic checks; results in `evals/results/<timestamp>/`.

`make lint` runs ruff. `uv run python -m retail_agent.observability.metrics` summarises
`logs/traces.jsonl`; `uv run python scripts/show_conversation.py <trace_id>` prints the full
message correspondence of a turn (the trace id is in every answer's footer);
`uv run python scripts/build_golden_index.py` re-embeds the golden trios after editing them;
`make requirements` regenerates the pip requirement files from `uv.lock`.

## Configuration

All settings come from the environment or `.env` (see `.env.example`): model and fallback
(`RETAIL_MODEL`, `RETAIL_FALLBACK_MODEL`), byte cap and row cap, per-turn budgets, timeouts.
The persona's shipped default is `config/persona.md`; changes made with `/persona set` go to
`data/persona.md` (versions in `data/persona_history/`); it is re-read on every turn. Identities and scopes are
`config/users.yaml` (production: a JWT from the frontend, see HLD).

Local state is git-ignored: `data/` (sessions, reports, preferences — SQLite; the runtime
persona) and `logs/` (`cli.log`, `traces.jsonl`).

## Layout

```text
src/retail_agent/
  query/          logical schema, SQL guard, demographics rule, scope binding, BigQuery executor
  agent/          ADK agent, tools, instruction, redaction, grounded figures, resilient model,
                  budgets, definitions, preferences, persona
  reports/        report store and the two-phase delete
  golden/         golden trio library and retrieval
  observability/  trace plugin, metrics, conversation rendering
  cli/            chat session and terminal app
golden/           trios (YAML) and the embedding index
config/           users and persona
evals/            scenarios, red team, harness
tests/            unit (offline) and live (BigQuery, Gemini)
scripts/          model probe, agent smoke run, golden index builder, conversation viewer
docs/             HLD, technical explanation, ADRs, eval results
```

## Limitations

- Small-group suppression (a minimum number of customers per aggregate row) is designed for
  production (BigQuery aggregation thresholds), not enforced in the prototype.
- The golden trios are a seed set written for the prototype, not reviewed by an analyst.
- Preferences are saved when the user states them; inferring them from behaviour is designed
  (HLD §5), not built. Charts are an extension point: a `charts` preference yields
  chart-ready tables.
- On the Gemini free tier (15 requests/minute for gemini-3.5-flash-lite, as reported by the
  API's 429 response) turns can be slow or fall back to the second model; a paid key changes
  only `.env`.
- Authentication is a `--user` switch; charts, email and web search are extension points.
