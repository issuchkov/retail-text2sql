# ADR-0002: Brand scope and PII rules live in the query layer

Status: accepted, 2026-10-29; amended 2026-11-03 with the client's answers (brands,
demographics only in aggregates)

## Context

Each manager may analyse only their brands; the CEO sees all. Names, emails, addresses,
postal codes and coordinates must never be shown or reach the model; demographics only in
aggregates. The model writes the SQL, so anything enforced only by instructions can be
talked around by a prompt injection or simply forgotten.

## Options

1. **Instructions to the model** — cheap, unenforceable.
2. **Filter results after the query** — the model has already chosen what to compute; hard
   to undo joins and aggregates over out-of-scope rows.
3. **Authorized views / row-level security per user in BigQuery** — strongest isolation, but
   needs views and policies created in the reviewer's project and per-user principals, which
   the prototype's single service identity does not have.
4. **Rewrite the query in the server**: the model writes SQL against logical table names; the
   server validates the AST and binds the names to brand-scoped, PII-free CTEs, with brands
   and salt as query parameters.

## Decision

Option 4 in the prototype, with option 3 added in production as a second wall.

## Consequences

- The model sees only the logical schema; base table names are rejected, so there is no way
  to reach unscoped data from SQL. Verified live: a manager sees exactly their brands and 0
  out-of-scope items.
- Customer-level analysis works on a salted pseudonym (`customer_key`), which joins orders
  and users but cannot be reversed without the salt.
- The demographics rule needs column lineage; the guard uses sqlglot's qualifier and scope
  model and fails closed when it cannot trace a column.
- Every query reads the base tables through CTEs (2–13 MB here) — fine for this dataset;
  production uses views and materialized aggregates.
- Small-group suppression cannot be done reliably by a post-hoc check; it is left to
  BigQuery aggregation threshold rules in production.
