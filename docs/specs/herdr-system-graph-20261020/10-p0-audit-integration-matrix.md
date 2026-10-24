# P0 — Repository Audit & Integration Matrix

**Spec:** `herdr-system-graph-20261020` (08-implementation-prompt.md, 07-phases-tests.md)
**Date:** 2026-10-21 (audit run directly in-session)
**Status:** Complete — this is the mandatory P0 gate deliverable: verified repo locations + integration matrix **before** writing code.

> P0 rule from spec: *"STOP if key repos unavailable; request access instead of inventing integrations."*
> All repos below were located and inspected read-only; no integration is asserted that was not verified in source.

---

## 1. Verified repository locations

| Repo | Path | Role in this spec | Availability |
|---|---|---|---|
| **Herdr Engineering** (canonical) | `/home/wohlgemuth/IdeaProjects/herdr-engineering` | Herdr dashboard + control plane + mission services | ✅ present, tests run |
| **Pi Engineering** (runtime) | `/home/wohlgemuth/IdeaProjects/pi-engineering` | mission/task/session/event contracts, worker execution | ✅ present |
| **InferWeave** | `/home/wohlgemuth/IdeaProjects/inferweave` | request traces, spans, model/GPU placement telemetry | ✅ present |
| **Artifact Studio / Libre WebUI** | no separate repo found; artifact tooling is **inside herdr-engineering** (`herdr_engineering/artifacts.py`, `control_plane/artifacts.py`, `packages/`) | persisted visual artifacts | ✅ present (in-repo) |

**Finding:** There is **no second orchestration service and no separate Artifact Studio repo**. Artifact authoring/storage already lives inside herdr-engineering. This satisfies the spec's non-goal *"No second orchestration service"* — the graph should extend the existing control plane, not spawn a sibling service.

---

## 2. Existing implementations to REUSE (not duplicate)

### 2.1 Event envelope & idempotent ingestion — `control_plane/events.py` (P1 contract ✓)
- `Event` dataclass: `event_id`, `schema_version`, `source_timestamp`, `ingest_timestamp`, `entity_type`, `entity_id`, `mission_id`, `session_id`, `sequence`, `event_type`, `payload` — **matches the spec's typed envelope** (event_id, schema_version, source, ingest, correlation, ordering, payload).
- `EventStore.append`: idempotent via `ON CONFLICT (event_id) DO NOTHING` (dedup by source event id — spec: *"upsert by source+event_id; tolerate out-of-order and duplicates"*).
- `EventStore.events_after(entity_type, entity_id, sequence)`: per-stream ordered replay (`ORDER BY sequence`) — spec: *"order by source sequence"*.

### 2.2 Lifecycle ingestion — `control_plane/ingest.py` (P1/P2)
- `IngestService.ingest(body)`: translates generic event envelopes into stable structured entities idempotently (hosts/sessions/missions + event streams). Repeated heartbeats never duplicate.
- `_git_identity`: strict forge-identity + full commit-SHA validation (P3 repository identity).

### 2.3 Structured event-sourced store — `control_plane/repo.py` (P2)
- `ControlPlaneRepo`: canonical in-memory domain model, event-sourced, replayable, unit-testable without Postgres; bridge to durable stores in `bridge.py`.

### 2.4 Mission/Session models — `control_plane/models.py` (P2)
- `MissionStore`, `SessionStore`: create/get/list/update on `missions`, `sessions`.

### 2.5 SQL schema — `deploy/control-plane/migrations/0001_control_plane.sql`, `0002_control_plane_enrichment.sql` (P2/P3)
- Tables: `hosts`, `host_samples`, `missions`, `mission_events`, `mission_stage_runs`, `plans`, `plan_revisions`, `sessions`, `session_events`, `agents`, `agent_runs`, `services`, … — **the graph projection can be materialized as Postgres views over these existing tables** (spec: *"Prefer Postgres tables/materialized adjacency views if sufficient"*).

### 2.6 HTTP + SSE surface — `control_plane/api.py` (P2/P4)
- REST under `/api/v1/*`, health, ingest route, and **SSE `/events/stream`** with a subscriber set (`repo.subscribe(self._broadcast)`) — spec's *"query, subscribe, replay"* is partially present.

### 2.7 Web UI — `packages/` (P2/P3/P4 dashboard)
- `topology-view.js`, `mission-catalog-view.js`, `mission-live-view.js`, `overview-view.js`, `services-view.js`, `sessions-view.js`, `request-*` surfaces, `herdr-web-client/client.js`, `herdr-web-components/*`. Existing Playwright tests in `tests/playwright/topology.test.mjs`.

### 2.8 Artifact tooling — `herdr_engineering/artifacts.py`, `control_plane/artifacts.py` (P5)
- Existing artifact publish/storage + upload API in `api.py` (`_upload_artifact`).

### 2.9 Redaction / security — `control_plane/redact.py` (P4/P6)
- `redact_payload`, `redact_text` for prompt/secret exclusion (spec security: *"secrets and private prompt contents excluded by default"*).

### 2.10 Trace fields — InferWeave `crates/iw-protocol/src/v1/requests.rs` (P4)
- `request_id`, `trace_id: Option<String>`, `span_id: Option<String>` present — cross-service trace correlation is feasible by mapping these into the event envelope.

---

## 3. Reuse / replace / extend map

| Spec phase | Build as | Reuses |
|---|---|---|
| **P1 Contract/prototype** | Extend `events.py` envelope with `correlation_id`/`trace_id`; add a graph-schema registry module + fixture-backed graph projection tests | `events.py`, `ingest.py`, migrations |
| **P2 Mission slice** | Add graph projection + node detail over existing `missions`/`sessions`/`mission_events`; reuse SSE subscription | `models.py`, `repo.py`, `api.py` SSE, `mission-*-view.js` |
| **P3 Repository slice** | Extend `_git_identity`/repo discovery for dependency parsing + cross-repo map + webhook/reconciliation | `ingest.py`, `service_catalog.py`, migrations |
| **P4 Request slice** | Map InferWeave `trace_id`/`span_id`/`request_id` into event envelope; build waterfall from sampled spans | `api.py`, `events.py`, InferWeave `requests.rs` |
| **P5 Artifacts** | Evaluate+pinned Archify renderer behind existing artifact tooling; add `/artifact` slash commands | `artifacts.py`, `control_plane/artifacts.py`, `packages/` |
| **P6 History/scale** | Replay/diff over event store; SLO/load tests; runbook | `repo.py`, migrations, `api.py` |

---

## 4. Baseline test result (P0 gate)

```
$ python3 -m pytest tests/control_plane/test_api.py tests/control_plane/test_repo.py \
    tests/control_plane/test_ingest.py tests/control_plane/test_models.py \
    tests/test_ci_correlation_ingest.py -q
101 passed in 19.18s
```

Core event/store/ingest/api components are green — a healthy baseline to extend.

---

## 5. Open items requiring confirmation (not invented)

- **Archify license/API** — not yet verified upstream; P5 must pin a revision and confirm license/deps before vendoring (spec requires this).
- **GitHub webhook availability** — not asserted; P3 refresh path must confirm webhook endpoint access.
- **Cross-host identity** — not verified; P4 trace correlation across hosts needs the identity mapping confirmed.

---

*This document lives under the unique spec path `docs/specs/herdr-system-graph-20261020/` and does not overwrite other specifications.*
