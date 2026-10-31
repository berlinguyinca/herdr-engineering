# ADR-0001 — Assistant domain lives on the existing control-plane repo

Status: **accepted** (P1/P2 slice) · Date: 2026-10-27

## Context

The spec (D2) forbids a second database and requires one canonical owner per
entity. The Herdr control plane already owns persistence via `ControlPlaneRepo`
(`herdr_engineering/control_plane/repo.py`), an in-memory collection store that
is the single persistence path the dashboard reads.

## Decision

The Personal Assistant domain (`assistant.py`) adds an `AssistantStore` whose
collections (assistants, threads, messages, runs, actions, grants, approvals,
memory) live on the same `ControlPlaneRepo`. No new DB server is introduced.
The store is wired into `ControlPlaneServer.__init__` as `server.assistant`.

## Consequences

- Positive: no duplicate persistence; assistant state is consistent with the
  rest of the dashboard and testable with the same in-memory repo.
- Negative: in-memory only for this slice; durability across a web restart is
  not yet provided. A Postgres-backed migration through `control_plane/db.py`
  `Migrator` is deferred to a later phase (tracked as remaining risk R1).

## Alternatives considered

- Postgres-backed assistant tables now: deferred to avoid touching migrations
  before the domain contracts are stabilized.
- A separate SQLite store: rejected (duplicate persistence; forbidden by D2).
