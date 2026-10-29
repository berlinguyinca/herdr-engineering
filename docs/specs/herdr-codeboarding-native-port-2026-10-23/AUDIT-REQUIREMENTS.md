# File-level audit requirements
Verified starting anchors from GitHub source inspection 2026-10-23; these are NOT a completed audit.

| Repo | Path | Inspect for |
|---|---|---|
| herdr-engineering | docs/architecture.md | host locality, data/control-plane boundary |
| herdr-engineering | herdr_engineering/control_plane/events.py | Event, EventStore, idempotency |
| herdr-engineering | herdr_engineering/control_plane/api.py | auth, routing, graph APIs, pagination |
| herdr-engineering | deploy/control-plane/migrations/0001_control_plane.sql and 0002_control_plane_enrichment.sql | existing schema, migrations, mission tables |
| herdr-engineering | packages/herdr-web-components/ | shared UI/design system, graph rendering |
| herdr-engineering | packages/herdr-web-client/ | HTTP/events and state |
| herdr-engineering | packages/session-workspace.js and session-workspace-model.js | workspace navigation and source links |
| herdr-engineering | herdr_engineering/control_plane/collab.py, collab_a2a.py, packages/collab-* | rooms, external agents, ACL |
| herdr-engineering | tests/control_plane/, tests/playwright/ | existing API and UI tests |
| pi-engineering | src/platform/WorkGraph.ts | project/run/task/worker/parent-worker/worktree IDs |
| pi-engineering | src/platform/eventstore/ | event backend, persistence, provenance |
| pi-engineering | src/runtime/herdr/HerdrAgentRuntime.ts, CollabLayout.ts | runtime/workspace/collab integration |
| CodeBoarding | entire source tree, tests, LICENSE, AGENTS.md | exact engine modules, JSON schema, incremental logic, license, telemetry |

Produce `audit.md` in extracted spec directory: current commit SHA, exact symbols, current behavior, tests, reuse/extend/refactor/port/reject, gaps, risks, chosen integration point. Search for existing architecture graph, renderer, APIs, caches, job scheduler, artifacts, GitHub integration and issue links before new code. Record why no duplicate is introduced.
