# ADR-0003: Gemini models — primary and fallback

Status: accepted, 2026-11-03

## Context

The agent needs a model that calls tools reliably and writes BigQuery SQL against the
logical schema (SPEC §4.3). Development runs on a free Gemini API key; production will use
a paid key (only `.env` changes). An agent turn needs several model calls, so latency and
the daily request quota matter as much as quality.

## Evidence (`scripts/probe_models.py`, free key, 2026-11-03)

Models available to the key include `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`,
`gemini-3.x-flash` up to 3.8 and `gemini-3.1-pro-preview`; `gemini-2.5-flash-lite`
returns 404 for new users.

One-word prompt, morning: `gemini-3.5-flash-lite` 49.8 s, 35.6 s, 47.2 s;
`gemini-3.1-flash-lite` 4.9 s on the same key and network.

Probe an hour later — 4 analysis questions per model, schema + `run_query` tool, SQL
checked by the guard and a BigQuery dry run:

| Model | Latency per call | Tool called | Guard + dry run |
|---|---|---|---|
| gemini-3.5-flash-lite | 0.7–1.1 s | 4/4 | 4/4 |
| gemini-3.1-flash-lite | 0.6–1.1 s | 4/4 | 4/4 |

Both used `customer_key`, the net revenue rule (`status NOT IN ('Cancelled','Returned')`
or `order_revenue`) and aggregated demographics without being corrected. 3.5 answered
"last full quarter" by first querying the quarter boundaries (a two-step plan); 3.1 wrote
the boundaries as literals in one query.

## Decision

- Primary: `gemini-3.5-flash-lite` (author's choice; 500 requests/day on the free tier).
- Fallback: `gemini-3.1-flash-lite` — equally correct in the probe, a separate quota, and
  fast while 3.5 was slow.
- Both configurable (`RETAIL_MODEL`, `RETAIL_FALLBACK_MODEL`).

## Consequences

- Free-tier latency swings by an order of magnitude within an hour → a per-call timeout
  (well below the 35–50 s seen) that switches to the fallback, plus retries on 429/5xx
  (SPEC §6, PLAN stage 6). This is the main reason the fallback exists.
- 500 requests/day bounds evals: a run of ~15 scenarios × 3–6 calls is 50–90 requests, so
  evals run once per change, not in loops.
- The probe is a smoke test (8 calls), not an eval: answer quality is measured in stage 9.
