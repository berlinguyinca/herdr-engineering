# ADR-0006 — Capability-gated, access-controlled reasoning visibility

**Status:** Accepted — 2026-10-16 (amends ADR-0005; does not supersede it)

## Context

ADR-0005 forbids persisting *hidden* chain-of-thought. The agent inspector
(spec 0200) needs a "reasoning" view for models/runtimes that **expose**
reasoning to the user (provider-surfaced summaries or raw thinking streams the
runtime already shows in its own UI, e.g. Pi's thinking panel).

## Decision

1. Reasoning is shown **only** when the emitting runtime declares
   `capabilities.reasoning = "summary" | "full"` and actually delivers it. The
   control plane rejects `reasoning` events otherwise. It never reconstructs,
   infers or summarises reasoning, and "Next" remains workflow-signal only.
2. Emitters default reasoning **off**; the Pi feed requires an explicit opt-in.
3. Reasoning bodies are access-controlled (default: `operator` role; configurable,
   fail-closed on unknown role names), excluded from search for lower roles,
   redacted with the standard rules, held only in bounded in-memory rings, and
   never written to the durable sink or the global activity feed.
4. The UI hides reasoning until the viewer opts in (per-agent or global
   preference) and states plainly when a model does not expose it.
5. Operators can disable reasoning storage entirely
   (`HERDR_INSPECTOR_STORE_REASONING=0`).

## Consequences

ADR-0005's prohibition stands for anything the runtime does not itself expose.
Hidden/private reasoning remains unavailable by construction.
