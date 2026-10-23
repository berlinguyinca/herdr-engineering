# T16 end-to-end acceptance evidence

Status: **evidence, not certification.** This document was produced by the agent that wrote the harness. It reports what
the automated suites do and do not prove; it is not a sign-off. A human reviewer should read the GAP list below before
treating any scenario as "done".

Scope: the seven scenarios of `ACCEPTANCE.md`, executed against real processes on loopback: the Herdr control plane
(`herdr-eng control-plane web`, started from a generated YAML fleet config), Pi modules and CLIs, and the real
`autospec` binary built from PR #4804. No live GitHub, no remote hosts, no paid models. Fakes (clearly headed
`FAKE - test shim`): `gh` shims (`tests/e2e/fake_gh/gh`, Pi `test/e2e/fake_gh_s2|s5/gh`), a fake Plannotator approver.
Two "hosts" share one plane (two ingest keys, sources, host ids, temp repos); server-to-server federation is T17.

## Branches

* Herdr: `feat/t16-release-gates` (worktree `herdr-t16`), Pi: `feat/t16-release-gates` (worktree `pi-t16`).
* **Stacked** on the open PRs listed in each PR body. Review only the T16 commits (`test(e2e)`, `feat(t16)`,
  `fix(redact)`, `docs`).

## Commands (exact)

```
# from the Herdr checkout root; ONE command for everything
scripts/run-acceptance.sh                       # add --build to build autospec, --only s2,s5 for a subset
scripts/run-acceptance.sh --results <file.json>

# old suites (regression)
PYTHONPATH=. python3 -m pytest tests/control_plane tests/test_central_config_servers.py -q

# Pi, per file (never --test-force-exit)
node --test --import ./test/support/isolatedState.ts test/e2e/<file>.e2e.test.ts
npx tsc --noEmit            # typecheck:core includes test/e2e
```

The runner fails on: any failure, zero tests in a suite, fewer tests than `tests/e2e/manifest.yaml`, strict-xfail
count above `max_xfail`, non-xfail skips, todo, or an unlisted e2e file (guarded by `test_runner_guards.py`).

## Result of the full run (2026-10-23)

`scripts/run-acceptance.sh` exit 0, wall clock 3m33s (Herdr side ~153 s, dominated by the Postgres chaos suite).

| scenario | runner | file | tests | pass | xfail / GAP-named |
|---|---|---|---|---|---|
| harness | pytest | test_smoke_harness.py | 2 | 2 | 0 |
| harness | pytest | test_runner_guards.py | 2 | 2 | 0 |
| s2 | pytest | test_s2_quorum.py | 17 | 17 | 0 |
| s3 | pytest | test_s3_room_graph.py | 8 | 8 | 0 |
| s4 | pytest | test_s4_external.py | 11 | 10 | 1 xfail |
| s5 | pytest | test_s5_steering_forks.py | 9 | 8 | 1 xfail |
| s6 | pytest | test_s6_chaos.py | 27 | 27 | 0 |
| s7 | pytest | test_s7_integrity.py | 23 | 23 | 0 |
| harness | node | smoke-plane.e2e.test.ts | 2 | 2 | 0 |
| s1 | node | s1-standalone-mission | 16 | 16 | 2 GAP |
| s2 | node | s2-autospec-mission | 19 | 19 | 5 GAP |
| s3 | node | s3-room-two-workspaces | 13 | 13 | 1 GAP |
| s4 | node | s4-external-bridge | 2 | 2 | 1 GAP |
| s5 | node | s5-fork-compare-promote | 15 | 15 | 3 GAP |
| s6 | node | s6-outbox-chaos | 5 | 5 | 0 |
| s6 | node | s6-stale-lease | 4 | 4 | 0 |
| s7 | node | s7-cross-repo | 6 | 6 | 2 GAP |

Totals: Herdr 99 collected (97 pass + 2 strict xfail); Pi 82 (82 pass, of which 14 are `GAP:` tests that pass by
asserting the *current, incomplete* behaviour; they are not evidence the feature works).

Regression: Pi `lineage-emitter` + `herdr-yaml-lineage` unit tests 30/30. Herdr regression command: 2425 passed, 3 skipped, 1 xfailed, 1 failed in 10m44 - the failure is the known flaky
`test_ci_api.py::test_telemetry_auth_limits_invalid_bundles_and_performance` (URLError). An earlier run also hit
`test_graph.py::test_neighborhood_stops_rendering_neighbours_once_the_budget_is_spent`, which passed alone and in this run
(load-sensitive; not touched by T16).

## Scenario -> evidence -> gaps

Rules: every refusal is paired with a positive control; `Plane.verify_features` proves the feature is on; sequences and
`gaps` are checked so refused events do not advance state.

* **S1 standalone mission (Pi alone, no AutoSpec)** - Pi `s1-standalone-mission` (16): real `plan --store` CLI, real
  mission ownership, lineage into a real plane, standalone merge-gate refusals, fake Plannotator approver.
  GAPs: plan CLI wires no emitter (plan/mission absent from graph); worker-review verdicts never emitted as `reviewed`
  edges; plan revision/digest/Plannotator decision not projected.
* **S2 AutoSpec mission with quorum** - Herdr `test_s2_quorum` (17) with the real `autospec review-quorum
  publish|request|verify-report`; Pi `s2-autospec-mission` (19): DAG executor boundary, PR creation via fake `gh`,
  quorum verdict mapping. Covers author-exclusion/self-approval, scope-limited approvers, stale evidence.
  GAPs: no CI `validated`/dependency edges from any runtime path; DAG issue ids not projected as issue nodes; Pi never
  calls `reconcileGithubLinks` nor reacts to `github.evidence_stale`; `decideMerge` has no runtime caller or quorum
  reader (test maps quorum to verdict itself); AutoSpec's review publisher writes org-scoped nodes room members cannot
  comment on.
* **S3 room, two workspaces** - Herdr `test_s3_room_graph` (8), Pi `s3-room-two-workspaces` (13): spawn edges parent ->
  child only, two sources gap-free, cross-workspace visibility rules. GAP: no Pi code emits tab/pane nodes (pane close
  covered only through raw ingest at the server contract).
* **S4 external agents** - Herdr `test_s4_external` (11): join/heartbeat/MCP surface, delegation bounds, consultation,
  no onward delegation by externals. Pi `s4-external-bridge` (2). GAPs: Pi has no external-agent bridge client (Herdr
  docs say none ships); MCP `tools/list` advertises the same menu to externals; strict xfail: `authorize` answers an
  existence oracle for workspace link ids; no `max_turns`/hop field (nearest: `max_steps`); consultation edge
  direction is thread -> subject.
* **S5 comment on failed review, fork, compare, promote** - Herdr `test_s5_steering_forks` (9), Pi
  `s5-fork-compare-promote` (15): real git repos with a bare remote, real forks, instance_id pinning ("same key under
  another instance_id is NOT offered"). GAPs: strict xfail - fork promotion marks no dependent gate stale; changed
  requirement text travels only as the 80-char fork label; promotion invalidates no gate or review (Pi side).
* **S6 chaos / restart / stale lease** - Herdr `test_s6_chaos` (27, throwaway Postgres cluster, server restart replay);
  Pi `s6-outbox-chaos` (5, swallowed-403 counted via `rejected` and dead-letter reason; crash-before-ack replay) and
  `s6-stale-lease` (4, takeover generation/fencing, stale identity refused, one logical agent across two hosts, stalled
  attempt keeps last reported state, rogue key cannot rewrite an agent node). Not tested in real time: credential
  expiry (300 s minimum; fake-clock in-process tests cover it). Stale review is a flag only, not a lease.
* **S7 integrity / cross-repo** - Herdr `test_s7_integrity` (23), Pi `s7-cross-repo` (6). GAPs: `emitDependencyEdges` /
  `buildDependencyGraph` have no runtime caller and the plane refuses dependency edges between only-"ensured" issue
  nodes; DAG cross-repo issue ids not projected.
* Not covered by HTTP: positive primary-room claim/terminal grant (needs a registered host; in-process only); removing a
  sponsor who is project manager has no HTTP route.

## Product fixes made because the e2e found them

1. `herdr_engineering/control_plane/redact.py`: `author`, `authors`, `author_login`, `author_name` were redacted
   (key contains "auth"), so PR-author self-approval exclusion could never match. Exempted via `_PLAIN_KEYS`.
2. Same file: the `sk-` secret pattern matched inside words (`task-s6lease` stored as `ta[REDACTED]`); now anchored
   with `(?<![A-Za-z0-9])`. Unit tests in `tests/control_plane/test_hardening.py`.
3. Pi glue (earlier commit `83b4bfb` and `dca36f3`): global-YAML `herdr:` block accepts `lineage`, `visibility`,
   `room_id`, `workspace_id`, `mission_id`; `createIngestFromConnection`, `createLineageFromConnection`,
   `loadLineageEmitter({agentDir})` prefer YAML; `resolveAttentionIngest` in `extensions/index.ts`; lineage nodes are
   stamped with `attrs.instance_id`.

## Caveats

* Pi plan CLI tests set `PI_CODING_AGENT_DIR`, Pi's own agent-dir input, not a configuration switch; fake `gh` shims are
  found through child-process `PATH` only.
* asyncpg must be installed for S6 (README prerequisites). Pi `biome check .` has pre-existing format failures in
  unrelated test files (e.g. `test/unit/attach-inspector.test.ts`); T16 files are clean.
* No flake campaign beyond the runs reported here; the Herdr regression has a known flaky telemetry test.
* Local-only: none of this ran in CI; Woodpecker integration of `run-acceptance.sh` is not part of this change.
