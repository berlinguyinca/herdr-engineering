# Execution log — P0–P6 run evidence (this task)

Timestamped evidence for the P0–P6 execution task. Every command below was run
in this session against the canonical repo at `HEAD == origin/main`
(`b30a634a`); all deliverables are committed, pushed, and byte-identical to
origin (verified with `git diff origin/main --stat -- docs/specs/herdr-system-graph-20261020`
→ empty). No new code change was required this session: the tree already
satisfies the phase gates, so this log records the gate runs (per the
"never mark complete without test + evidence" rule).

## Environment
- Repo: `/home/wohlgemuth/IdeaProjects/herdr-engineering` (canonical, per P0 audit §1)
- `HEAD` = `origin/main` = `b30a634a`; spec dir + implementation files tracked and clean.
- Python 3 (`python3 -m pytest`).

## Gate runs (all green)

| # | Gate (07-phases-tests.md) | Command (module set) | Result |
|---|---|---|---|
| 1 | P0 baseline green | `test_api.py test_repo.py test_ingest.py test_models.py` + `tests/test_ci_correlation_ingest.py` | **182 passed** (33.7s) |
| 2 | P1 contract — schema compat | `tests/control_plane/test_graph.py` (full) | **79 passed** (suite); 72 core + registry widening |
| 3 | P1 contract — duplicate / out-of-order replay | `test_graph.py -k "idempotent_replay or out_of_order or shuffled_replay"` | **3 passed** |
| 4 | P1 permissions | `test_graph_membership.py` | **30 passed** |
| 5 | P1 permissions (scope/authorization) | `test_graph.py -k "scope_only or authorized_and_scoped or non_owner"` | **3 passed** |
| 6 | P2 mission slice (E2E + offline reconnect) | `test_graph_slices.py::TestMissionSlice` | **4 passed** |
| 7 | P3 repository slice (dep parsing, rename) | `test_graph_slices.py::TestRepoSlice` | **4 passed** |
| 8 | P4 trace slice (real-trace mapping, honesty) | `test_graph_slices.py::TestTraceSlice` | **3 passed** |
| 9 | P5 artifact (deterministic snapshot, malicious HTML, CSP) | `test_graph_artifact.py` | **4 passed** |
| 10 | P6 history/impact/rollback | `test_graph_history.py::TestHistorySlice` | **4 passed** |
| 11 | P6 SLO/load + failure recovery | `test_graph_history.py::TestSloScale` | **2 passed** |
| 12 | Full graph module suite | `test_graph{,_slices,_artifact,_history}.py` | **101 passed** (19.2s) |
| 13 | All control-plane graph tests | `tests/control_plane/test_graph*.py` + work_graph + artifact_graph_routes | **216 passed** (37.3s) |
| 14 | Live-ingest wiring (opt-in) | `test_graph_ingest.py` | **10 passed** |
| 15 | API surface | `test_artifact_graph_routes.py test_work_graph_api.py` | **25 passed** |

## CLI gate (P5, end-to-end this session)
```
$ python3 -m herdr_engineering.cli graph-artifact render /tmp/snap.json --out /tmp/snap.html
{"ok": true, "out": "/tmp/snap.html"}
$ grep -c "Content-Security-Policy" /tmp/snap.html   # → 1
$ sha256sum snap.html snap2.html                     # render twice
02b4de8e0db2eb…609ebfd1  /tmp/snap.html
02b4de8e0db2eb…609ebfd1  /tmp/snap2.html             # byte-identical → deterministic
```

## Reuse verification (source-checked this session)
- Emitters own **no store**; they apply through the existing
  `GraphProjection.apply` (`graph.py:1008`, commutative + idempotent) —
  `graph_mission.py:114`, `graph_repo.py:129`, `graph_trace.py:108`.
- No parallel event schema: emitters build stored-style envelopes via the
  existing `from_stored`/`validate_lineage` path (`graph.py:1016`).
- History/diff/impact are pure functions over `GraphProjection.snapshot()`
  (`graph.py:2385`); rollback = replay of the prior event window
  (runbook §4.5), exercised by `test_rollback_equals_prior_snapshot`.
- P1 "correlation_id/trace_id envelope" is satisfied by the existing lineage
  envelope + InferWeave `trace_id`/`span_id` mapping in `graph_trace.py`
  (`from_inferweave`, `graph_trace.py:113`) — no second envelope added.

## Blockers / notes
- 6 control-plane test files error at collection locally due to missing
  `psycopg2` (`test_artifact_library_api`, `test_assistant_team_briefs*`,
  `test_*_external_actions*`) — environment-only, unrelated to graph code;
  ignored for these gate runs.
- Full `tests/control_plane/` suite exceeds a single 25-minute wall budget on
  this host (long e2e tests); gates above were run as targeted suites instead.
- ETA: gate re-verification ≈ 5 min; no implementation remaining for P0–P6.

## 2026-11-02T05:05Z — Repair pass: deploy routing verification (F-FBqUeG/F-788Oib)

- Finding: review reported an empty change. Verified the delivered spec commit is
  `b87ea6d7` (`docs(spec): P0-P6 execution log — gates run 2026-11-02`, +50 lines,
  `git show --stat b87ea6d7`); `git diff b87ea6d7 origin/main` is empty, so the
  candidate is byte-identical to `origin/main`.
- Additional gate re-run in this session:
  `tests/test_deploy_scripts.py tests/test_ci_gate_routing.py` → 187 passed, 0 failed.
- `deploy/control-plane/deploy.sh` working-tree state: routing stub that execs the
  canonical `herdr-deploy` entrypoint (`command -v herdr-deploy` →
  `/home/wohlgemuth/.local/bin/herdr-deploy`, resolves to the `b30a634a` deploy-tool
  checkout). `bash -n` passes on both the stub and the `HEAD` (`git show
  HEAD:deploy/control-plane/deploy.sh`) copy. The stub is intentionally not committed:
  it hard-codes an operator-local path and is not part of the spec delivery.
