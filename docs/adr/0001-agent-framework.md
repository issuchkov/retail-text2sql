# ADR-0001: Google ADK as the agent framework

Status: accepted, 2026-10-29

## Context

The agent needs tool calling with Gemini, a human confirmation step before destructive
tools, hooks before and after model and tool calls (budgets, redaction, tracing),
persistent sessions, and an instruction that can change without a redeploy. The author has
over a year of production experience with both Google ADK and PydanticAI.

## Options

- **Google ADK** — Gemini-native; tool confirmation (`request_confirmation`,
  `adk_request_confirmation`) built in; agent callbacks and plugins; session services
  (in-memory, database, Vertex AI); callable instructions; OpenTelemetry spans; BigQuery
  tools.
- **PydanticAI** — excellent typing and testing (`TestModel`), model-agnostic; confirmation
  and tracing would be built by hand or via Logfire.
- **LangGraph** — explicit state machines; more machinery than a single-agent tool loop needs.

## Decision

Google ADK. Its confirmation flow, plugin hooks and session services cover R3, R5 and R7
with framework features rather than custom plumbing, and Gemini is the client's preferred
model.

## Consequences

- The ADK BigQuery toolset's `execute_sql` is **not** used for row data: scope binding and
  PII rules must run before results reach the model (ADR-0002). ADK supplies the loop; the
  query layer is our code.
- The confirmation round trip was verified with a scripted model before building on it
  (PLAN stage 3 decision log). One subtlety found: ADK calls the model again right after the
  pause, so the tool result must say that nothing has been deleted yet.
- Tests drive the real ADK runner with a scripted `BaseLlm`, so wiring is tested offline.
