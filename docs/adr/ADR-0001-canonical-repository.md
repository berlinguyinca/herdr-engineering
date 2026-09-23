# ADR-0001 — Canonical HerdR Engineering repository

**Status:** Accepted — 2026-09-23

## Decision

Use `berlinguyinca/herdr-engineering` as the canonical repository for the Super HerdR integration project.

The earlier proposed `berlinguyinca/herdr-powerpack` repository is superseded before implementation. “Powerpack” remains an internal name for the curated dependency/plugin distribution portion of HerdR Engineering.

## Why

The requirements grew beyond a plugin bundle into a coherent distributed engineering cockpit: fleet bootstrap, mobile/web, browser/review, shared artifacts, session activity, transparent dev services, tests, CI, multi-worktree comparison, observability and safe lifecycle management. Keeping separate repositories would create duplicated configuration and unclear ownership.

## Consequences

- One README and implementation program explains the complete product.
- External plugins stay external and pinned; they are not vendored merely because this is one repo.
- Changes required in Pi Engineering, AutoSpec or Pileated occur in those owning repos behind versioned contracts.
