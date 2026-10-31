# Test matrix

Status key: **exists** (found, not run), **planned**, **blocked**. Nothing here is a pass result yet.

| Scenario | Spec | Test | Status |
|---|---|---|---|
| 10+ sequential handoffs | 4, 10.1, 10.9 | Pi `test/unit` sequence over `MissionStore` with real `transitionHandoffRecord` | planned (S3) |
| Cross-model handoff | 4 | Pi `plannerWorker/sessionHandoff.ts` tests (`model-select-handoff.test.ts`) | exists, unread; extend |
| Cross-host recovery | 4, 10.7 | lane claims `lanes-git.test.ts:80-92`; host attr `lineage-emitter.test.ts:344` | partial; add host-move graph test |
| Parallel branching | 5, 10.2 | `test_forks.py` (31) | exists; add two-worktree drive |
| Supervisor replacement | 2 | not assessed in audit | planned |
| Interrupted checkpoint | 4 | `orchestration-checkpoints.test.ts:169-357`; crash-per-handoff-stage | partial / planned (S4) |
| No duplicate worktree writers | 4 | `orchestration-repository-lease.test.ts`, `git-worktree-concurrency.test.ts` | exists; add worktree lock |
| Artifact preservation | 10.5 | commit emits graph artifact, survives restart | **run, pass**: `test_artifact_lineage.py`, `test_artifact_scope.py` (rebuild test) |
| Lineage after restart | 10.8 | `test_graph.py:461`, `test_s6_chaos.py:90` | exists (e2e needs Postgres) |
| Authorization isolation | 10.6 | `test_graph_membership.py`, `test_artifact_access.py`, assistant `read_lineage` scope tests | partial; assistant planned (S1) |
| Reviews bound to SHA | 5 | `autospec crates/autospec-core/tests/review_quorum.rs` | exists in AutoSpec |
| CI failure, repair, re-review | 10.4 | `test_ci_repair_rereview_gate.py` | **run, pass** (existing pieces; production loop not wired) |
| Assistant observation | 7A, 10.3 | `read_lineage` tests | planned (S1) |
| Assistant controlled delegation | 7B | grant/approval negative tests incl. self-approval | planned (S0, S6) |
| Duplicate/out-of-order events | 10.8 | `test_graph.py:93,133,749` | exists |
| Desktop/mobile UI | 6 | `tests/playwright` ui-gates (workbench, states, visual baselines); Missions page and assistant Proposals/Notifications/Approvals baselines (3 of 6 viewport/scheme combinations each, size cap) plus `ui-gates-dashboard-a11y.test.mjs` (ratchet over known findings) | partial (S5): lineage/workbench, Missions and assistant tabs have baselines; tablet, mobile dark, non-default Missions tabs and mission detail do not |
| 60+ sessions bounded render | 10.9 | `ui-gates-perf` (2k-10k nodes, server-capped at 3,000 loaded), `ui-gates-workbench` 64-session chain, `tests/perf/lineage-scale.mjs` (5k/10k loaded in the client); numbers in `perf.md` | exists |

## How to run locally (bender)
`python3` only (no `python`). Fresh venv: `pip install -e '.[dev,collab,review-gate,federation]'`. `python3 -m pytest --co -q` collected 8152 of 8281 in about 17 s. JS: `npm ci` then `npm run test:client`. UI gates: `bash scripts/ci/ui-gates.sh` (about 11 min; baselines were made with Chromium 156, cached browsers here are 1228/1243/1248, so pixel diffs may need the pinned build). CI of record is Woodpecker (`.woodpecker/`).
