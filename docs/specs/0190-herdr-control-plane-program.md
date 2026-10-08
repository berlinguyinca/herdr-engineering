# 0190 — HerdR Dev Fabric Control Plane (program roadmap)

Status: **proposed** · Owner: wohlgemuth · Started: 2026-10-06

This is the **program-level roadmap** for the "HerdR Dev Fabric Control Plane"
spec (the user-supplied 116-section spec, hereafter "the Spec"). It is the
north star and the mapping from the Spec onto concrete, independently
shippable sub-plans. Each phase below is a self-contained plan that produces
working, testable software on its own (writing-plans skill: one plan per
subsystem). Do **not** attempt all phases in a single change set.

The unified product: one coherent engineering workspace at `http://dev.lan`
integrating HerdR, pi-engineering, Planator, development hosts, missions,
sessions, services, artifacts, telemetry, token/resource accounting,
analytics, and live interaction with running Pi sessions. It consumes the
same structured APIs/events as the rest of the platform — it is a client, not
an orchestration authority.

## Guiding principles (Spec §116)

- Identity is stable. Location is dynamic.
- Execution is distributed.
- State is structured (no UI scraping, no logs-as-primary-API).
- Events are ordered and replayable.
- Artifacts are addressable.
- History is durable.
- The UI is a client.
- The design system is shared.
- Color is semantic, not decorative.
- PostgreSQL remembers structured history. RustFS remembers artifact bytes.

## Scope guardrails (Spec §3, §114)

- No SQLite. No large binaries in PostgreSQL.
- No Redis / NATS / Kafka / extra distributed infra just because this is
  distributed. Prefer the existing HerdR fabric/event mechanisms.
- Do not create a separate web-only Pi conversation; the browser attaches to
  the same live session.
- Do not equate Mission with Session. Do not use hostname/path/PID as
  identity. Do not expose permanent RustFS/S3 credentials to agents.
- Do not delete hosts/sessions/services when connectivity is lost.
- Do not derive core state by scraping terminal/Docker/Slurm/HTML output.

## Phase map (Spec §113)

| Phase | Name | Spec sections | Deliverable | Status |
|-------|------|---------------|-------------|--------|
| 1 | Foundation | §4, §49–58, §90, §95, §108, §114, §115 | Postgres+migrations, RustFS, entity IDs, event envelope/sequencing/ingestion, mission/session/service models, state snapshots, artifact model, background-worker scaffold | **done** (11 commits) |
| 2 | Shared Design System | §6–16, §92 | `packages/herdr-design-system`, `herdr-web-components`, `herdr-web-client`; tokens, typography, AppShell, shared components | **done** (node-tested) |
| 3 | Core UI | §20–34, §42–47, §85, §91, §92 | `/`, `/overview`, `/missions`, `/missions/:id`, `/herd`, `/herd/session/:id`, `/services`, `/hosts`, `/activity` using design system | **done** (hash-routed SPA + web-serving tests) |
| 4 | Live HerdR/Pi interaction | §43–45, §52, §62 | session streaming (SSE/WS), message send, approve/reject, steer/interrupt/resume/stop, tool/log/diff/test views | **done (SSE + messages/actions)** |
| 5 | Artifacts | §56–74, §102 | RustFS integration, drag/drop/paste, routing, per-host cache, session materialization, Pi notification, generated artifacts | **done (upload/download + materialize)** |
| 6 | Host & fabric telemetry | §23–25, §89, §105 | CPU/RAM/disk/network/GPU, host+service health, leases, control-plane health, attention queue | **done (telemetry + leases + health)** |
| 7 | Analytics | §75–84, §96 | token + resource accounting, stage durations, duration/token histograms, model analytics, repair/retry analytics | **done (mission/model/host analytics)** |
| 8 | Hardening | §87–88, §96–104, §106, §110 | permissions, secret redaction, retention, backups, restore tests, failure recovery, performance, a11y, mobile | **done (RBAC, redaction, retention, backup/restore, reduced-motion)** |

**Durable bridge (§106, §114-115)** — `control_plane/bridge.py` hydrates the
in-memory repo from Postgres (`load_into(repo, source)` + `PostgresSource`),
so the API/UI can serve real persisted state via `--pg-dsn`. Pure mapping,
fake-tested; asyncpg lazy/graceful.

**Remaining (needs operator access — not runnable from this environment):**
- Live deploy of the compose stack to `bender` (no SSH key available here).
- Live attach of a running Pi session to the browser (structured client +
  SSE implemented; transport wiring to a live session is operator-run).
- RustFS production image swap (MinIO S3 drop-in stands in).

## Route inventory (Spec §5)

```
/                    /overview
/missions            /missions/:mission_id
/herd                /herd/session/:session_id
/services            /services/:service_id
/hosts               /hosts/:host_id
/activity
/analytics           /analytics/missions /analytics/models /analytics/hosts
```

## Core entity model (Spec §17–19)

Host · Workspace · Repository · Worktree · Mission · Plan · PlanRevision ·
Session · AgentRun · Service · Artifact · ArtifactBinding ·
ArtifactMaterialization · TestRun · Review · ReviewFinding · PullRequest ·
TelemetrySample · Event · AuditEvent · Snapshot. Every major entity has a
stable ID (Spec §18: `mission_id`, `session_id`, `service_id`,
`artifact_id`, `host_id`).

## Event protocol (Spec §48–51)

Standardized events (MissionCreated…ArtifactFailed), envelope
(`event_id`, `schema_version`, `source_timestamp`, `ingest_timestamp`,
`entity_type/id`, optional `mission_id`/`session_id`, `sequence`,
`event_type`, `payload`), globally-unique ids, idempotent ingestion,
deterministic sequencing per logical stream, UTC timestamps.

## Storage (Spec §4, §53–57)

- **PostgreSQL** — structured state (missions, plans, sessions, hosts,
  services, events, telemetry, token usage, analytics, artifact metadata,
  provenance, audit). JSONB + FKs + indexes; time-partition high-volume
  tables where justified.
- **RustFS** — object bytes, canonical bucket `herdr-artifacts`,
  content-addressed keys `sha256/ab/cd/<full-sha256>`, application identity =
  `artifact_id`. Integrity via SHA-256.

## Definition of Done (Spec §115)

37 acceptance conditions — the authoritative checklist. Each phase plan must
map its tasks back to the relevant DoD conditions; the program is complete
when all 37 hold.

## Execution handoff

Phase 1 has its own detailed plan:
`docs/superpowers/plans/2026-10-06-herdr-control-plane-phase1-foundation.md`.
Phases 2–8 receive their own plans when their predecessor is merged.
