# 0000 — Master Program: Super HerdR / HerdR Engineering

## Objective

Build `berlinguyinca/herdr-engineering` from an empty repository into a reproducible, private, cross-machine engineering cockpit around Herdr. The result should let one operator supervise dozens of persistent Pi/engineering sessions and worktrees across multiple machines from desktop, phone or tablet, while keeping source ownership, model routing, CI and review in their proper systems.

This master spec consolidates the earlier Herdr runtime-integration, distributed-fleet, Powerpack/plugin, shared-workspace/session-panel, transparent-dev-fabric and testing/CI specifications into one dependency-gated program.

## Product boundary

HerdR Engineering owns:

- curated/audited dependency and plugin distribution;
- declarative fleet installation/configuration;
- fleet-wide projections/navigation built on native Herdr machine primitives;
- web/mobile integration;
- browser/preview/review integration;
- shared artifact/handoff space;
- normalized session activity/timeline projection;
- transparent ephemeral dev-service addressing;
- normalized test explorer and CI views;
- candidate/worktree comparison UX;
- health, security, observability, notification, update and rollback integration.

It does not own:

- persistent terminal/runtime implementation — Herdr;
- agent mission/DAG/review orchestration — Pi Engineering;
- specs/issues workflow — AutoSpec;
- inference/GPU scheduling — Tern;
- CI execution — Woodpecker/Pileated;
- review semantics — Plannotator;
- Git truth — Git/GitHub;
- Pi-specific UI implementation — Pi-Web.

## User journeys that must work at release

### 1. Start remote work and leave

An operator launches Pi work on `bender`, closes the laptop, later opens HerdR Web on an iPad and sees the same live session, current state, activity summary and worktree. The remote process has continued uninterrupted.

### 2. Dozens of UI candidates

Pi Engineering fans one mission into isolated worktrees across several hosts. Each candidate launches its existing random-port React/Next/Vite server. HerdR Engineering assigns a fleet-wide lease and exposes each preview through `dev:<port>`, then groups diff/test/CI/review/browser evidence so candidates can be compared without remembering hosts.

### 3. Debug a failed test

The Tests view shows repository → suite/class/module → test hierarchy. A failed test opens its trace, log artifacts and exact worktree source line. The operator reruns the single test using the native framework command and can hand the failure context to the owning Pi session.

### 4. Understand CI pressure

The CI view shows Woodpecker/Pileated pipeline state, steps, queue and runners. A failed pipeline links back to the Git revision, related session/activity and test evidence. Supported retry/cancel actions are permission-gated and executed by the CI provider.

### 5. Know what every agent is doing

Each session has a bounded newest-first timeline and Started / Completed / Current / Next projection. Evidence is clickable. No hidden reasoning is captured.

## Global invariants

1. Never build a second source of truth for a concern already owned by another system.
2. Never silently fall back to terminal scraping if a structured source exists.
3. Never expose internal development/debug services publicly by default.
4. Never persist secrets or hidden chain-of-thought.
5. Never make shared storage a multi-writer source checkout.
6. Never hard-code the validation host list into application behavior.
7. Keep external dependencies replaceable behind HerdR Engineering contracts/adapters.
8. All externally sourced plugins/dependencies are pinned after compatibility/security/license audit.
9. Any control action is authenticated, authorized, auditable and idempotent where practical.
10. Failure of one optional plugin or one remote host must not take down the rest of Herdr.

## Program sequence

| Phase | Outcome |
|---|---|
| 0010 | repository/reconciliation foundation |
| 0020 | verified upstream compatibility matrix |
| 0030 | stable internal contracts over native Herdr |
| 0040 | declarative four-host fleet bootstrap |
| 0050 | pinned Powerpack distribution/lifecycle |
| 0060 | private web/mobile control surface |
| 0070 | browser preview + Plannotator integration |
| 0080 | shared artifact/handoff workspace |
| 0090 | session journal/timeline projection |
| 0100 | semantic Pi Engineering/AutoSpec correlation |
| 0110 | transparent `dev:<port>` fabric |
| 0120 | normalized cross-language Tests explorer |
| 0130 | Woodpecker/Pileated CI view |
| 0140 | multi-agent worktree/candidate comparison |
| 0150 | coherent unified engineering UX |
| 0160 | attention/notifications/review actions |
| 0170 | observability/security/update/rollback |
| 0180 | full fleet validation, docs and release |

## Completion rule

Do not declare the project complete until `docs/acceptance-matrix.md` is fully evidenced. A genuine external blocker may be marked `BLOCKED_EXTERNAL`, but must include evidence, affected acceptance rows, a tracked issue and a clear safe degraded behavior.
