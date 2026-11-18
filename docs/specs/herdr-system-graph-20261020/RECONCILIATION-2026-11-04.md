# Reconciliation: herdr-system-graph-20261020 (P0 audit)

Date: 2026-11-04. Worker: implementer. Scope: herdr-system-intelligence spec set
vs. the live herdr-engineering repo (canonical bundle).

## 0. Scope limitation (honest)

- Herdr binary: NOT found on this host (`herdr` not on PATH; no
  `herdr-engineering` install dir; fleet hosts bender/beast/mac.lan are not
  reachable from this worktree). Per spec 07 P0 this is a STOP condition for
  live-integration work (P2 live graph, P4 distributed traces). The CLI
  inspection step is blocked; recorded as a blocker, not faked.
- Upstream repos: Archify is not present in the repo and no license/pin was
  found (`grep archify` → no Python source hits). P5 therefore cannot pin an
  upstream revision; a local adapter contract stub is the only honest P5
  deliverable available in-worktree.
- Pi Engineering / InferWeave sources: not present in this worktree; P4
  (distributed traces across the three systems) is blocked on their event
  schemas.

## 1. Reuse / extend map (what exists vs. what the spec wants)

| Spec need | Existing implementation | Decision |
|---|---|---|
| Stable entity ids, namespaced | `control_plane/ids.py` (Crockford base32, `kind_` prefix; kinds: mission, session, service, artifact, host, agent, plan, worktree, event, lease, llmkey) | Extend: add `node`, `edge`, `snapshot`, `request`, `trace` kinds |
| Event envelope + dedupe | `control_plane/events.py` (EventStore, async, source_sequence), `repo.record_event` (per-entity seq, source_timestamp, ingest_timestamp, event_id, schema_version) | Extend: add correlation_id/causation_id/trace_id/payload_hash to envelope; add upsert-by-(source,event_id) semantics in graph module |
| Ingestion endpoint | `POST /api/v1/ingest` → `IngestService` (host/session/mission/service/inspector + generic fallback) | Extend: add `entity_type=graph` with node/edge payloads |
| Graph projection over existing storage | none (audit confirmed: no graph module, no graph API routes, no graph tables) | NEW `control_plane/graph.py`: projection engine that derives nodes/edges from repo state (missions/sessions/hosts/services/artifacts/test runs/reviews/PRs) + explicit graph event upserts |
| Graph API | `api.py` resource dispatch (`_api_get`/`_api_post` on `/api/v1/<resource>`) | Extend: `GET /api/v1/graph`, `GET /api/v1/graph/nodes/{id}`, `GET /api/v1/graph/nodes/{id}/neighbors`, `POST /api/v1/graph/nodes`, `POST /api/v1/graph/edges`, `GET /api/v1/graph/snapshots` |
| Node/edge metadata (source system, observed_at, effective_from/to, revision, confidence, provenance, staleness, last_verified_at, observed-vs-inferred) | none | NEW, in `graph.py` |
| Schema registry (typed node/edge kinds, cardinality, direction) | none | NEW, in `graph.py` (embeds the 02-graph-contract kind lists) |
| Durable persistence | Postgres migrations via `Migrator` over `deploy/control-plane/migrations/`; in-memory repo + DurableEventSink as bridge | Extend: migration `0003_graph.sql` (graph_nodes, graph_edges, graph_events, graph_snapshots) |
| Snapshots | `control_plane/snapshots.py` (SnapshotStore: save/load_latest/replay over fabric_events) | Reuse pattern; graph snapshots get stable snapshot ids (`new_id("snapshot")`) and replay from graph events |
| Retention | `retention.py` (TTL prune telemetry/events/artifacts) | Extend: graph nodes/edges are durable; only raw graph event frames are aged |
| RBAC | api roles viewer/operator/ingest; ingest endpoint open to `ingest` role | Graph reads = viewer; graph writes = operator (consistent with existing POST policy, spec 0300 §104) |
| Artifacts (P5) | `control_plane/artifacts.py` + artifact upload/download with storage_key/sha256 + RustFS | Extend later; adapter contract only this pass |
| Tests | 683 passed / 4 skipped / 1 xfailed baseline (`python3 -m pytest -q`, 125s) | Add `tests/control_plane/test_graph.py` |

## 2. Deliverable status after this pass (honest)

- P0 audit: done (this document + ledger entries).
- P1 contract: schema registry, event envelope extensions, graph projection
  fixture, API routes, idempotent upsert/dedup, permissions — implemented and
  tested (test_graph.py).
- P2 mission slice: projection covers mission → session → host/service/PR/
  review/test-run edges with source links from existing repo state; UI wiring
  not done (out of this pass — requires web-client change + live Herdr).
- P3 repo slice: NOT started (needs repo discovery + dependency parsing infra;
  no existing implementation found to reuse).
- P4 request slice: BLOCKED (Pi/InferWeave schemas unavailable; see §0).
- P5 artifacts: BLOCKED on Archify availability (no upstream in worktree).
- P6 rollout: NOT started.

## 3. Progress publication (spec 07 requirement)

Features accepted / total (spec set): P0+P1 contract = 2 of 7 phase gates met.
Tests: see run output in worker_result evidence. Blockers: Herdr binary absent,
Archify absent, Pi/InferWeave absent. ETA: P2 UI + P3 within 1-2 working
sessions if live access is granted; P4/P5 cannot be dated until dependencies
land. Confidence: low-to-medium (dependency-gated).
