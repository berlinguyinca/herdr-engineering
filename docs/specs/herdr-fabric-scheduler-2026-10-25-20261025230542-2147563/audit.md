# Herdr Fabric Scheduler P00 audit

## Provenance

- Execution host: `bender` (Linux); Mac origin: SSH alias `macbook-pro`, remote hostname `mac.lan`.
- Archive origin: `/Users/wohlgemuth/Downloads/herdr-fabric-scheduler-2026-10-25.zip`.
- Archive SHA-256: `85c84d7f72c9e668aca47e0196a8ce9af75cefa87bc13a44403b4d1d9beae39c`.
- Extraction: this directory only; the verified ZIP prefix is `herdr-fabric-scheduler-2026-10-25/`.
- Repository branch: `main`; HEAD and dirty-worktree state were captured before scheduler edits. Existing dirty control-plane/publication work is preserved.

## Current architecture and reusable seams

| Concern | Existing implementation | Scheduler integration |
| --- | --- | --- |
| Structured state/event stream | `herdr_engineering/control_plane/repo.py:ControlPlaneRepo`, `record_event`, `subscribe` | Add scheduler projections/events here rather than a second store. |
| Host registration/telemetry | `repo.py:upsert_host`, `record_heartbeat`, `record_telemetry`, `list_telemetry`; `control_plane/ingest.py:IngestService._ingest_host` | Extend host records with stable UID/policy/telemetry generations and keep generic ingestion compatible. |
| Durable deployment schema | `deploy/control-plane/migrations/0001_control_plane.sql` host/host_samples tables plus current 0003/0004 migrations | Add one scheduler migration for policies, reservations, decisions, leases, history. |
| HTTP API/auth | `control_plane/api.py:_api_get/_api_post`, role gate in `RequestHandler._require` | Add `/api/v1/scheduler/*` under existing operator/read scopes; no duplicate listener. |
| Native Herdr transport | `herdr_engineering/herdr_adapter.py:NativeHerdrAdapter`, `Machine`, `machine_targeted` | Placement selects a machine UID; native pane/workspace APIs remain the launcher. |
| Existing host-load helper | `herdr_engineering/fabric.py:_host_load_per_core` | Reuse only as a fallback diagnostic; scheduler telemetry must be structured and freshness-aware. |
| Fleet discovery/config | `herdr_engineering/fleet.py`, `fleet_config.py`, `scripts/fleet_inventory.py` | Stable UID and aliases are attached to existing fleet identity, not a new inventory. |
| Collaboration fleet launcher | `deploy/control-plane/collab-spawn.py:Runner.discover/reachable/pick_host/herd_pane/place/process_one` | Replace `pick_host` and inherited `parent_host` behavior with shared placement requests. |
| Collaboration permissions | `control_plane/collab.py` fleet host/spawn methods and tests | Keep room authorization separate from host execution authorization. |
| Dashboard | `packages/app.js` hash routes, `packages/fleet-view.js`, `packages/topology-view.js`, `packages/hosts-view.js` if present, shared Herdr components/CSS | Extend Hosts/Fleet route and client API, retain dark responsive shell and SSE. |
| Model/GPU scheduling | InferWeave integration and `docs/operations/agent-summary-fabric.md` | Scheduler reports GPU facts/requirements but never owns inference worker placement. |

## Existing WIP and compatibility

The working tree contains unrelated artifact/publication control-plane edits and an untracked `cleanmainwt/` checkout. Scheduler work must not reset, stash, or rewrite those files. Legacy `auto_agents=false` remains a hard manual-only mapping. Existing host labels remain display aliases; stable UIDs become the authorization identity. Existing generic host heartbeats remain accepted and produce conservative unknown/stale scheduler state until trusted scheduler telemetry is present.

## P00 findings

1. `collab-spawn.py` is a direct round-robin placement path and explicitly forces child placement onto the parent's host (`parent_host or pick_host`), violating the new lineage/placement invariant.
2. Existing host identity is host-label based and aliases are read-time only; it cannot detect duplicate machine identity or prove transport ownership.
3. Existing telemetry is a small CPU/memory/disk projection with no freshness, sequence, PSI, reservation, or confidence semantics.
4. No existing reservation/admission service exists; adding one in `herdr_engineering/scheduler.py` plus repository projections is the smallest consolidated seam.
5. Existing dashboard APIs and SSE are sufficient for scheduler views; no second SPA or event stream is justified.

## P00 decision

Implement a pure, testable scheduler domain (`host_policy.py`, `telemetry.py`, `scheduler.py`) with repository-backed persistence and a single API adapter. Integrations call the domain through a `PlacementAdapter`; launch mechanics remain native Herdr/collaboration code. Migration is feature-flagged and disabled until checkpoint/fencing tests prove safety.

