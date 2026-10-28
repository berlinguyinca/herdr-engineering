# ADR-0003 — Versioned runtime adapter; deterministic skeleton for P1/P2

Status: **accepted** (P1/P2 slice) · Date: 2026-10-27

## Context

The spec (02-architecture, 06-rakazo-adaptation) requires running the assistant
behind a **versioned adapter** and not vendoring Rakazo's `packages/core`
wholesale. It also forbids building a parallel model router. The InferWeave
route is the canonical inference path.

## Decision

Introduce `RuntimeAdapter` (versioned interface with `execute(run, messages)`)
in `assistant.py`. The default implementation for the P1/P2 vertical slice is
`LocalRunnerAdapter`, a deterministic, explicitly-labeled skeleton that composes
a reply from the persona, the user message and any granted memory. It calls no
external model and is **never** a production inference path. Runs execute
synchronously: `submitted → authorized → running → completed | failed`.

## Consequences

- Positive: chat and runs are fully testable end-to-end now; the adapter seam
  is exactly where an InferWeave-backed runner plugs in during P3.
- Negative: the assistant does not yet produce real model responses; async
  queueing, checkpoint/resume and real routing are P3+ (risk R2). The skeleton
  reply is clearly marked `[local-skeleton runner vN]` in the UI so it is never
  mistaken for production output.

## Alternatives considered

- Wire InferWeave now: deferred; the model route, quotas and provider wiring
  belong to a later phase per the dependency order (08-phased-plan).
