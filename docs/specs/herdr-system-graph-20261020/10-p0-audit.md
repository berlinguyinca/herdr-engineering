# P0 — Inventory & audit (evidence-backed)

Status: **complete** (2026-10-21, bender). Every location below was verified on disk or via the
GitHub API on the stated date; nothing is asserted from the design spec alone.

## 1. Verified repository locations and access

| System | Local path | Remote | Head at audit | Access |
|---|---|---|---|---|
| Herdr (dashboard + control plane) | `~/IdeaProjects/herdr-engineering` | `git@github.com:berlinguyinca/herdr-engineering.git` | `2e11f2c` (main, clean) | read+write, push+PR |
| Pi Engineering (orchestration runtime) | `~/IdeaProjects/pi-engineering` | `git@github.com:berlinguyinca/pi-engineering.git` | `513f781` | read+write, push+PR |
| PI WEB (plugin host for operator surface) | `~/IdeaProjects/pi-web` | (local checkout) | — | read |
| PI WEB mission plugin (read-only surface) | not checked out locally | `git@github.com:berlinguyinca/pi-engineering-web` | — | read via API |
| InferWeave (Rust, 368-crate workspace) | `~/IdeaProjects/inferweave` | `git@github.com:InferWeave/inferweave.git` | `bd1efff6` | read+write |
| Archify (candidate renderer, external) | not vendored | `github.com/tt-a1i/archify` | v3.2.0, MIT, pushed 2026-10-21 | public |

**"Libre WebUI" — UNRESOLVED (open question).** No repository, service, or doc named "Libre"
exists under `berlinguyinca`, locally, or in any prior herdr spec (grep of `docs/` returns only
this new spec's own files). Closest verified candidates:
- `pi-engineering-web` — standalone PI WEB plugin, **read-only** observation of missions via
  `.pi-eng/orchestration-snapshot.json` (contract v1–v3).
- `herdr-engineering/packages/` — hand-rolled JS dashboard served by the control plane.

**P0 rule applied:** not invented. The Artifact Studio renderer target (spec 05) must be pinned to
one of these (or a repo we don't have access to) before P5. Default assumption for planning:
herdr dashboard owns new tabs; PI WEB plugin gets read-only graph projection.

## 2. What already exists (reuse map)

### 2.1 Event transport & stores — REUSE, extend

| Layer | Existing implementation | Evidence |
|---|---|---|
| Mission event store (authoritative) | Event-sourced JSONL per worktree: `~/.local/state/pi-engineering/worktrees/<id>/events/<session>.jsonl`; event types `mission.created/updated`, `task.*`, `execution.*`, `lease.*`, `finding.*`, `evidence.*` | inspected live streams (4900+ events/session) |
| Mission schema | `pi-engineering/src/orchestration/types.ts` — `MissionStatus`: 29 states incl. resilience states (`WAITING_FOR_LLM/CAPACITY/GATEWAY/MODEL`, `RECOVERING_CONTEXT/PROCESS`, `PAUSED_INFRASTRUCTURE`, `NEEDS_ATTENTION`); `WorkflowClass`; `WorkspaceManifest` | `types.ts:24` ff. |
| Canonical lifecycle | `src/orchestration/canonicalLifecycle.ts` (LIFECYCLE_SCHEMA_VERSION) | file present |
| Cross-repo ingestion bridge (built) | `pi-engineering/src/runtime/herdr/ControlPlaneIngest.ts` — best-effort, non-blocking, idempotent upsert on `(entity_type, entity_id)`; entities host/mission/session; POSTs lifecycle events to control plane | file header documents contract |
| Pi activity bridge (built) | `herdr_engineering/mission_activity_feed.py` — bounded read-only native Pi telemetry over Unix socket, field allowlist, redaction via `control_plane/redact.py` | module docstring |
| Control-plane event envelope | `herdr_engineering/control_plane/events.py` — `Event(event_id, schema_version, source_timestamp, ingest_timestamp, entity_type, entity_id, mission_id, session_id, sequence, event_type, payload)`; "idempotent ingestion, repeated heartbeats never duplicate" | `events.py`, `ingest.py` docstrings |
| Durable store | Postgres (`control_plane/db.py`, DSN via `HERDR_CP_PG_*`), artifacts in RustFS/S3 bucket `herdr-artifacts` (`rustfs.py`) | `db.py` defaults |
| Telemetry (OTel-friendly, not OTel SDK) | `pi-engineering/src/lifecycle/telemetry.ts` ("Events are OpenTelemetry-friendly"); `src/runtime/isolation/runtimeEvents.ts` → `sessions/<id>/runtime.jsonl` + bounded ring + telemetry sink | file headers |
| Artifact store (pi side) | `pi-engineering/src/artifacts/ArtifactStore.ts` — file-based, sha256-verified, locked, immutable `artifact://` refs | file header |

**Envelope gap vs spec 02:** the live mission envelope has `event_id, timestamp, type, run_id,
worker_id, payload, session_id, worktree_id, sequence` but **no** `correlation_id`, `causation_id`,
`trace_id`, `tenant_id`, `payload_hash`. Spec 02 requires all of these. → P1 must extend the
envelope (additive, versioned) without breaking existing consumers.

### 2.2 Dashboard / UI — EXTEND

- `herdr_engineering/control_plane/api.py` — stdlib HTTP API, role-based (operator / read /
  ingest), rate-limited; `GET /api/v1/events/stream` (**subscribe already exists**, SSE-style);
  native message dispatch with cross-host machine routing.
- `packages/` — framework-less JS views: `overview-view.js`, `mission-live-view.js`,
  `mission-catalog-view.js`, `topology-view.js` (list-based record table — **no node-edge canvas
  graph exists anywhere in the fleet**; grep for cytoscape/d3/react-flow/dagre/vis-network: none),
  `sessions-view.js`, `services-view.js`, `ci-charts.js`, `integrations-view.js`,
  `herdr-design-system/`, `herdr-web-components/`.
- Deployment: `deploy/control-plane/` container exposed on `dev.lan` (Tailnet trust boundary,
  port 80); also `deploy/fabric-stack`, `deploy/mission-feed`, `deploy/herdr-cp-feed-plugin`,
  `deploy/pi-build-gate`.
- PI WEB plugin: `pi-engineering-web` reads `.pi-eng/orchestration-snapshot.json` (v1–v3) — a
  precedent for a versioned read-model contract the graph API can reuse.

**Gap vs spec 04:** nine dashboard tabs required; today there are ~8 list/table views and no
graph canvas. A graph rendering library must be introduced (spec 01: "Use existing live graph
canvas library if already present" — verified: none present).

### 2.3 Infrastructure / topology — REUSE (InferWeave side)

- InferWeave is a 368-crate Rust workspace; `contracts/` already contains
  `topology-graph.example.json`, `topology-v2.example.json`, `link-topology-heartbeat.example.json`
  — a link/node topology concept for the inference fleet (hosts, GPUs, models, workers).
- `schemas/` = json-schema + openapi. Crates of interest: `iw-gateway` (queue wait, routing),
  `iw-scheduling`, `iw-relay`, `iw-store`, `iw-protocol(-http)`.
- **Gap vs spec 03/04 (P4):** no OpenTelemetry SDK anywhere in pi-engineering; no
  `trace_id`/`span_id` correlation between Pi missions and InferWeave requests today.
  Correlation must be added (mission `run_id` → gateway request id mapping) — new work, not reuse.

### 2.4 Cross-host identity & discovery — REUSE

- Private Ansible inventory (not committed): `ansible/inventory/hosts.yml` → fleet =
  `beast`, `bender` (linux) + `mac` (macos), managed by `scripts/fleet_inventory.py`, per-host
  `host_vars/`.
- Control plane already models hosts (`host.registered` / `host.heartbeat` ingest) and routes
  native messages per machine.
- GitHub: API access verified (`gh` authenticated); **no webhooks configured** on
  `berlinguyinca/herdr-engineering` or `berlinguyinca/pi-engineering` (GitHub API, 2026-10-21).
  Spec 03's "webhooks + incremental scans" → P3 must create webhooks + a receiver (new work).

### 2.5 Archify (spec 05) — verified facts, P5 evaluation

- `tt-a1i/archify`: **MIT**, JavaScript, v3.2.0, pushed 2026-10-21, ~87k stars, 130k+ skills.sh
  installs; an **agent skill** that produces **interactive HTML** diagrams from
  specs/descriptions; works with Claude Code/Codex/Cursor/OpenCode.
- Unverified (spec 05 requires before promising): per-family support (architecture/dependency/
  sequence/dataflow/class/ERD/timeline/waterfall), deterministic layout metadata, SVG/PNG export,
  interaction hooks, CSP/sandboxing story, transitive deps, performance. → P5 gate item.

## 3. Integration matrix (spec requirement → action)

| Spec area | Reuse | Extend | Create |
|---|---|---|---|
| 01 topology | existing stores (Postgres, RustFS, JSONL event streams, OpenViking memory) | — | graph projection service (read model) |
| 02 contract | `events.py` envelope, `types.ts` lifecycle | additive envelope fields (correlation/causation/trace/tenant/payload_hash), schema registry | node/edge type registry with provenance+ACL fields |
| 03 ingestion | `ControlPlaneIngest.ts`, `mission_activity_feed.py`, `ingest.py` | idempotency/tombstones, dead-letter + operator replay, offline buffer+badge | Git webhook receiver, 15-min reconciliation, daily dependency reconciliation |
| 04 dashboard | `packages/` UI + design system, `api.py` roles/rate limits, `/events/stream` | new tabs (Mission Graph, Repository Map, Request Flow, …) | node-edge canvas lib (none exists), 10k-node virtualization, timeline/replay |
| 05 artifacts | `ArtifactStore.ts` (artifact://), RustFS, PI WEB snapshot-contract precedent | `/artifact …` pi slash commands | Archify adapter (pinned rev, sandboxed HTML), snapshot versioning vs graph snapshot IDs |
| 06 security/scale | control-plane RBAC + redaction, Tailnet boundary, fleet inventory | ACLs on graph query/subscribe/export, per-source provenance filtering | load/SLO validation harness, feature flag + one-click disable, canary rollout |
| Cross-cutting | Postgres, RustFS, ansible fleet, `gh` API | — | **trace correlation layer (Pi↔InferWeave)** — nothing exists |

## 4. Test baselines (measured, 2026-10-21, bender)

| Repo | Command | Result | Duration |
|---|---|---|---|
| herdr-engineering | `.venv/bin/python -m pytest tests/ -q` | **1081 passed, 4 skipped, 1 xfailed** | 219 s |
| pi-engineering | `npm run test:unit` (node --test) | **4181 passed, 0 failed, 10 todo (4191 total)** | 248 s |
| inferweave | `cargo metadata` (parse check only; full `cargo test` deferred to P4) | workspace OK, 368 packages | <60 s |

Baseline commits: herdr `2e11f2c`, pi `513f781`, inferweave `bd1efff6`.

## 5. Blockers / open questions (carry into P1)

1. **Libre WebUI identity** (spec 00/05/07 name it; nothing by that name exists). Decision needed:
   herdr dashboard tab vs PI WEB plugin vs a repo we lack access to. **Blocks P5 target surface only.**
2. **No webhooks anywhere** — P3 requires GitHub webhook creation + receiver endpoint (needs a
   publicly/tailnet-reachable ingress for the control plane).
3. **No trace correlation** — P4 requires new instrumentation on both sides (Pi run_id ↔
   InferWeave request ids).
4. **No graph canvas library** — P1/P4 must choose one (eval: rendering 10k nodes, clustering,
   semantic zoom, bundle size, license) — spec 01 permits it only after audit, which is now done
   (none present → selection is allowed).
5. Worker-backend instability (see Engineering Ledger `FINDING-DIcT30`) affects mission-pipeline
   execution capacity, not this audit's evidence.

## 6. Progress & ETA (measured)

- P0 features accepted: **9/9** audit items from spec 09 "Must verify in P0" (1 unresolved → open
  question #1, per spec: stop and ask rather than invent).
- Elapsed P0 wall time: ~25 min agent time (excl. 7.4 min of test runs).
- Evidence links: this file + event-stream paths in §2.1 + baseline table in §4.

ETA ranges for remaining phases (confidence: medium, based on reuse map — most integration
plumbing already exists; canvas graph + trace correlation are the novel 60%):
- P1 (contract/prototype): **1–2 days**
- P2 (mission slice, end-to-end event→UI): **2–3 days**
- P3 (repository slice + webhooks): **3–5 days**
- P4 (request slice + trace correlation): **4–7 days**
- P5 (artifacts + Archify): **3–5 days** (Archify per-format verification is the long pole)
- P6 (history/scale/rollout): **4–7 days** (load tests + canary + runbook)
- **Total P1–P6: ~3–4 weeks** single-contributor, quality gates included.
