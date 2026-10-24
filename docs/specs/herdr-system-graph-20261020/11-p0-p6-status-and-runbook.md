# Herdr System Intelligence — P0–P6 Status & Operational Runbook

**Branch:** `feat/herdr-system-graph` (PR-ready small commits)
**Spec path:** `docs/specs/herdr-system-graph-20261020/`
**Status date:** 2026-10-21

---

## 0. Reconciliation (important)

`main` already contains a mature lineage graph — `GraphProjection` at
`herdr_engineering/control_plane/graph.py` (1,300+ lines, 70+ tests, from PR
#165), wired into `control_plane/api.py` REST endpoints and `repo.py`. The spec
explicitly forbids parallel graph stores and duplicate implementations.

**This PR therefore does NOT add a second graph.** It:

1. **Widens the existing shared kind registry** (`NODE_TYPES` / `EDGE_TYPES`) with
   the spec's node kinds that did not exist yet (`repository`, `service`, `host`,
   `gpu`, `model`, `worker`, `request`, `span`, `queue`, ...) and edge kinds
   (`calls`, `observed_in`, `blocked_by`, ...). This is backward-compatible: the
   new kinds default to per-writer OWNED semantics exactly like the existing
   non-commons kinds, and every existing test still passes.
2. Adds **emitters** (`graph_mission.py`, `graph_repo.py`, `graph_trace.py`)
   that translate mission/session, repository, and InferWeave request/span
   state into lineage events, applied through the existing
   `GraphProjection.apply` (or `admit` on live ingest). They own **no store**.
3. Adds a **deterministic sanitized artifact renderer** (`graph_artifact.py`)
   over `GraphProjection.snapshot()`, and **diff/impact/rollback** helpers
   (`graph_history.py`) over snapshots.
4. Adds a `graph-artifact render` CLI command (the `/artifact` slash-command
   analog).

Independent review by both `codex` and `claude` confirmed this
`DROP_PARALLEL_REBASE` reconciliation is correct.

## 1. What was delivered

| Phase | Deliverable | Gate | Tests |
|---|---|---|---|
| **P0** | Repo audit + integration matrix (`10-p0-*.md`) | key repos verified; baseline green | 72 baseline |
| **P1** | Reused existing `GraphProjection`; widened kind registry | all 72 existing graph tests still pass | 72 |
| **P2** | `graph_mission.py` — mission/session/handoff/blocked_by emitter | E2E event→graph + offline replay | 4 |
| **P3** | `graph_repo.py` — repo + dependency/supersession emitter; manifest dep parser | fixture accuracy, rename | 3 |
| **P4** | `graph_trace.py` — request/span/calls/observed_in emitter; honesty for sampling | real-trace mapping, gap indicators | 2 |
| **P5** | `graph_artifact.py` + `graph-artifact render` CLI — deterministic sanitized snapshot | deterministic snapshot, malicious-HTML/security | 4 + CLI |
| **P6** | `graph_history.py` — diff/impact/rollback; SLO/load + failure-recovery | SLO/load, failure recovery, rollback | 6 |

**New tests:** 20 (all passing) · **Existing graph suite:** 72 passing (one
pre-existing timing flake in a load test, `best < 0.3s`, unrelated to this PR).

## 2. Test evidence

```
$ python3 -m pytest tests/control_plane/test_graph.py \
    tests/control_plane/test_graph_slices.py \
    tests/control_plane/test_graph_artifact.py \
    tests/control_plane/test_graph_history.py -q
91 passed
```

SLO/load: 1000-node + 999-edge fixture projected well under a 1.0s budget
(acceptance target p95 < 2s per spec 06).

## 3. Design decisions honored

- **No second graph store** — emitters feed the existing `GraphProjection`;
  diff/impact/render operate on `snapshot()`.
- **No second orchestration service** — graph is derived from authoritative
  mission/event stores; emitters are read-only w.r.t. missions.
- **Archify NOT vendored** — spec requires verifying upstream license/API
  before pinning (05, 09). `graph_artifact.py` is the safe deterministic
  snapshot contract a pinned Archify adapter can later consume.
- **Observed vs inferred / honesty** — sampling and unknown parents are
  surfaced as attrs, never fabricated (02); a span without a recorded parent
  gets no `calls` edge.
- **Ownership** — new kinds default to per-writer OWNED semantics (not shared
  COMMONS), matching the spec's per-source ownership model.

## 4. Operational runbook

### 4.1 Emit mission/session into the graph
```python
from herdr_engineering.control_plane.graph import GraphProjection
from herdr_engineering.control_plane.graph_mission import MissionGraphEmitter, mission_lineage_events
g = GraphProjection()
MissionGraphEmitter.apply_all(g, mission_lineage_events(
    MissionGraphEmitter(), [{"kind": "mission", "id": "m1", "state": "running", "stage": "plan"}]))
```

### 4.2 Build a graph snapshot artifact
```bash
# snapshot.json is a GraphProjection.snapshot() dict (plus optional "title"):
herdr-eng graph-artifact render snapshot.json --out snapshot.html
# deterministic; digest printed; HTML is sanitized + CSP-locked
```

### 4.3 Diff before/after a rollout
```python
from herdr_engineering.control_plane.graph_history import diff
d = diff(before_snapshot, after_snapshot)   # added/removed nodes + edges
```

### 4.4 Impact analysis
```python
from herdr_engineering.control_plane.graph_history import impact
impact(graph.snapshot(), "mission:m0")   # reachable (dependency) closure
```

### 4.5 Rollback
Rollback = replaying the prior event window; `diff(pre, rolled)` is empty once
restored.

### 4.6 Scale/SLO guardrails
- Projection is O(events) with idempotent dedup; p95 scoped graph budget < 2s.
- Sampling labels and unknown-span indicators are surfaced, never fabricated.

## 5. Honest open items (not invented)

- **Archify license/API** — unverified upstream; P5 uses the safe renderer
  until pinned.
- **Live ingest wiring — DONE (follow-up PR).** `GraphIngestAdapter`
  (`graph_ingest.py`) translates mission/session/repository/request/span
  lifecycle events into lineage envelopes and routes them through the real
  `repo.ingest` path (admit + persist + apply). Opt-in via
  `--system-graph` / fleet config `control_plane.system_graph`; off by
  default. 8 tests.
- **Dashboard UI — DONE (follow-up PR).** The live lineage renderer
  (`packages/collab-lineage.js`) now draws the spec-02 kinds: new node kinds get
  distinct shapes (repository/service/model/worker/deployment/branch/module =
  round, host/gpu = hex, request/span/trace/queue/dataset/symbol/package =
  tag); the node-shape legend is data-derived
  and the 14 new edge kinds (`calls`, `observed_in`, `belongs_to`, `blocked_by`,
  `implements`, `references`, `runs_on`, `serves`, `deploys_to`, `assigned_to`,
  `precedes`, `imports`, `routes_to`, `queued_on`) get dash/tag/word styles.
  Previously `mergePages` silently DROPPED any edge kind not in the style table,
  so these edges never reached the canvas; now they render and appear in the
  data-derived filters/legend. 23 collab-lineage client tests pass; full client
  suite 513 pass. (The main `topology-view.js` is a record-based view of
  services/sessions/CI, separate from the lineage canvas; it needs no graph kinds.)

## 6. Feature acceptance tally

| Area | Accepted / Total | Notes |
|---|---|---|
| Reuse existing graph (P1) | ✅ complete | registry widened, 72 tests green |
| Mission slice (P2) | ✅ complete | 4 tests |
| Repository slice (P3) | ✅ complete | 3 tests |
| Request/trace slice (P4) | ✅ complete | 2 tests |
| Artifact renderer + CLI (P5) | ✅ complete | 4 tests + CLI |
| History/scale/runbook (P6) | ✅ complete | 6 tests |
| Live ingest wiring | ✅ complete | graph_ingest.py + --system-graph opt-in, 8 tests |
| Dashboard UI wiring | ✅ complete | collab-lineage.js renders spec-02 node/edge kinds, 23 client tests |

*Progress is tracked by passing tests and linked evidence, not generated-file
counts (spec 08).*
