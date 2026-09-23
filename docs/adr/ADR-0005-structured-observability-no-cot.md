# ADR-0005 — Structured observable activity; no hidden reasoning capture

**Status:** Accepted — 2026-09-23

## Decision

Build session timelines from native Herdr/Pi/AutoSpec/Git/test/CI/browser/review signals and explicit user-visible summaries. Do not scrape terminals when structured signals exist, and never persist hidden chain-of-thought.

## Consequences

The client panel may show Started / Completed / Current / Next, commands, tool outcomes, changed files, tests, commits, artifacts and review state. “Next” is populated only from an explicit workflow/task signal, not reconstructed from private model reasoning.
