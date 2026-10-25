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
  `fix(redact)`, `docs`); sibling PRs: UI gates #178 (separate scope), security fixes #179.

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
count above `max_xfail`, Pi `GAP:` tests above `max_gap`, an xfail that is not `strict=True`, non-xfail skips, todo, or an unlisted e2e file (guarded by `test_runner_guards.py`).

## Result of the full run (2026-10-23, after the review round)

`scripts/run-acceptance.sh` exit 0, wall clock 7-10 min, load dependent (Herdr side dominated by the Postgres chaos suite). The runner prints
**pass / strict-xfail / GAP as separate columns**; a GAP test passes by asserting the *current, incomplete* behaviour and is
not counted as a pass.

| scenario | runner | file | tests | pass | strict xfail | GAP |
|---|---|---|---|---|---|---|
| harness | pytest | test_smoke_harness.py | 2 | 2 | 0 | - |
| harness | pytest | test_runner_guards.py | 4 | 4 | 0 | - |
| s2 | pytest | test_s2_quorum.py | 19 | 19 | 0 | - |
| s3 | pytest | test_s3_room_graph.py | 8 | 8 | 0 | - |
| s4 | pytest | test_s4_external.py | 14 | 10 | 4 | - |
| s5 | pytest | test_s5_steering_forks.py | 14 | 13 | 1 | - |
| s6 | pytest | test_s6_chaos.py | 27 | 27 | 0 | - |
| s7 | pytest | test_s7_integrity.py | 23 | 23 | 0 | - |
| harness | node | smoke-plane.e2e.test.ts | 2 | 2 | - | 0 |
| s1 | node | s1-standalone-mission | 16 | 14 | - | 2 |
| s2 | node | s2-autospec-mission | 20 | 15 | - | 5 |
| s3 | node | s3-room-two-workspaces | 13 | 12 | - | 1 |
| s4 | node | s4-external-bridge | 2 | 1 | - | 1 |
| s5 | node | s5-fork-compare-promote | 19 | 16 | - | 3 |
| s6 | node | s6-outbox-chaos | 6 | 6 | - | 0 |
| s6 | node | s6-stale-lease | 5 | 5 | - | 0 |
| s7 | node | s7-cross-repo | 6 | 4 | - | 2 |

**TOTAL: 181 pass, 5 strict-xfail, 14 GAP** (Herdr 111 tests = 106 pass + 5 xfail; Pi 89 tests = 75 pass + 14 GAP). Before
the review round it was 97 + 2 xfail (Herdr) and 82 (Pi, 14 GAP).

The runner's last line says "no failures ... 19 KNOWN GAPS remain": it never says "all good" while gaps exist. Per-file
caps live in `tests/e2e/manifest.yaml` (`max_xfail`, `max_gap`), so a failing test cannot be renamed `GAP:` or turned into
an xfail without a reviewed diff; every Herdr xfail must be `strict=True` (checked by AST in the runner, with guard tests).

Other suites run in this round: Pi `test/unit/herdr-yaml-lineage.test.ts` 10/10, `lineage-emitter` 21/21; `npx tsc
--noEmit` clean; Herdr regression (`tests/control_plane tests/test_central_config_servers.py`) 2425 passed, 3 skipped,
1 xfailed, 1 failed (the known flaky `test_ci_api` telemetry test) in the previous round; an earlier run also hit
`test_graph.py::test_neighborhood_stops_rendering_neighbours_once_the_budget_is_spent`, which passed alone and in the later
full run.

### Mutation check of the review round

Each new test was shown to FAIL against the exact mutant the independent review found surviving (mutant applied to a copy
of both worktrees, only the named code changed):

| review finding | mutant | killed by |
|---|---|---|
| steering fence at propose | `collab_workbench.py` propose `compare_digest(efence, fence)` -> `if False` | Herdr `fence_at_propose`, Pi `fence at PROPOSE` |
| steering fence at confirm (expected_fence) | confirm drops the `efence` comparison | Herdr `fence_at_confirm_a_confirmer_who_read...`, Pi `fence at CONFIRM` |
| steering fence at delivery | delivery drops `_fence_of(facts) != r["fence"]` | Herdr `fence_at_delivery`, Pi `fence at DELIVERY` |
| all three fences removed | the three edits together | 4 Herdr tests and 3 Pi tests |
| self-confirmation | `proposer == pr.id and not self_confirm` -> `if False` | Herdr `an_owner_cannot_confirm_their_own_proposal...`, Pi `self-confirmation` |
| untrusted writer sets PR author | `trusted("author")` accepts any writer | Herdr `an_untrusted_writer_cannot_set_or_blank_the_pr_author`, `without_a_trusted_author...` |
| same-owner stale lease | `assertSameEpoch` ignores generation/fencing token | Pi `the SAME owner id re-acquiring after expiry...` |
| merge gate check sha | `check.sha !== prHeadSha` -> `false` | Pi `a GREEN CI check pinned to the previous head...` |
| YAML visibility default | `conn.visibility ?? "private"` -> `"org"` | Pi unit `visibility defaults to PRIVATE` |

Not killed / not added: event-id ledger replay (H12), external action cap `EXTERNAL_ACTIONS` (H20), foreign node write
`admit` (H22), lineage-key room scope at the Herdr ingest layer (the Pi side kills it, the Herdr e2e does not),
credential and `hard_expiry` expiry (see "Expiry coverage" below). These remain open.

## Scenario -> evidence -> gaps

Rules: every refusal is paired with a positive control; `Plane.verify_features` proves the feature is on; sequences and
`gaps` are checked so refused events do not advance state.

* **S1 standalone mission (Pi alone, no AutoSpec)** - Pi `s1-standalone-mission` (16): real `plan --store` CLI, real
  mission ownership, lineage into a real plane, standalone merge-gate refusals, fake Plannotator approver.
  GAPs: plan CLI wires no emitter (plan/mission absent from graph); worker-review verdicts never emitted as `reviewed`
  edges; plan revision/digest/Plannotator decision not projected.
* **S2 AutoSpec mission with quorum** - Herdr `test_s2_quorum` (19) with the real `autospec review-quorum
  publish|request|verify-report`; Pi `s2-autospec-mission` (20): DAG executor boundary, PR creation via fake `gh`,
  quorum verdict mapping. Covers author-exclusion/self-approval, an untrusted key setting or blanking the PR `author` (and the `unknown_author` fail-closed state), scope-limited approvers, stale evidence, a green check pinned to the previous head.
  GAPs: no CI `validated`/dependency edges from any runtime path; DAG issue ids not projected as issue nodes; Pi never
  calls `reconcileGithubLinks` nor reacts to `github.evidence_stale`; `decideMerge` has no runtime caller or quorum
  reader (test maps quorum to verdict itself); AutoSpec's review publisher writes org-scoped nodes room members cannot
  comment on.
* **S3 room, two workspaces** - Herdr `test_s3_room_graph` (8), Pi `s3-room-two-workspaces` (13): spawn edges parent ->
  child only, two sources gap-free, cross-workspace visibility rules. GAP: no Pi code emits tab/pane nodes (pane close
  covered only through raw ingest at the server contract).
* **S4 external agents** - Herdr `test_s4_external` (14): join/heartbeat/MCP surface, delegation bounds, consultation,
  no onward delegation by externals. Pi `s4-external-bridge` (2). GAPs: Pi has no external-agent bridge client (Herdr
  docs say none ships). Four strict xfails in Herdr `test_s4_external`: `authorize` answers an existence oracle for
  workspace link ids; MCP `tools/list` advertises the same menu to every external (including always-refused tools);
  no `max_turns`/`max_hops` in a delegation budget (nearest: `max_steps`); the consultation edge runs thread -> subject, not
  asker -> target.
* **S5 comment on failed review, fork, compare, promote** - Herdr `test_s5_steering_forks` (14), Pi
  `s5-fork-compare-promote` (19): real git repos with a bare remote, real forks, instance_id pinning ("same key under
  another instance_id is NOT offered"). Fences are tested one stage at a time (propose / confirm / delivery) by changing only a fenced attribute, and self-confirmation is refused with an operator-switch control. GAPs: strict xfail - fork promotion marks no dependent gate stale; changed
  requirement text travels only as the 80-char fork label; promotion invalidates no gate or review (Pi side).
* **S6 chaos / restart / stale lease** - Herdr `test_s6_chaos` (27, throwaway Postgres cluster, server restart replay);
  Pi `s6-outbox-chaos` (6: a SERVER kill -9 with Pi up, a real separate Pi OS PROCESS killed with SIGKILL whose durable queue a new process replays gap-free, duplicates, dropped event, revoked key, swallowed-403 counted via `rejected` and dead-letter reason) and
  `s6-stale-lease` (5, same-owner older generation/token refused, takeover generation/fencing, stale identity refused, one logical agent across two hosts, stalled
  attempt keeps last reported state, rogue key cannot rewrite an agent node). Stale review is a flag only, not a lease.
* **S7 integrity / cross-repo (server and Pi half only)** - Herdr `test_s7_integrity` (23), Pi `s7-cross-repo` (6). GAPs: `emitDependencyEdges` /
  `buildDependencyGraph` have no runtime caller and the plane refuses dependency edges between only-"ensured" issue
  nodes; DAG cross-repo issue ids not projected.
* **S7 UI half: covered by a separate PR, not by this one.** Scenario 7 also demands keyboard-only use, mobile layout,
  reduced motion, large-graph performance, screenshots and visual regression of the workbench. None of that is in the
  suites above. It is built by the UI-gates work: branch `feat/t16-ui-gates`, PR #178, `docs/operations/ui-gates.md`
  (`npm run test:ui-gates`). Until that PR is reviewed and its gates are green, the UI half of scenario 7 is NOT
  evidenced here; run-acceptance.sh does not run it.
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

## Expiry coverage (limits)

Real credential expiry cannot be waited out in an e2e run: the shortest agent credential TTL is 300 s and the control plane
reads its own clock. The e2e suites therefore cover **server-side state timeouts that the YAML can shorten** (unconfirmed
steering proposal, confirmed-but-undelivered request, stale review heartbeat, expired invite: `test_s6_chaos.py`
`TestExpiryAndStaleness`) and revocation, but NOT credential expiry or an external agent's `hard_expiry` in real time. Mutants
that disable them (`agent credential expiry`, `hard_expiry`) survive the e2e suites. They are covered by in-process
fake-clock tests in the regression suite: `tests/control_plane/test_collab_pentest.py::test_agent_credential_expiry_and_device_binding_enforced`,
`::test_mcp_expired_and_revoked_agent_and_owner_loss`, `::test_expired_grant_and_expired_membership_cut_agent_off`,
`tests/control_plane/test_collab_bounded.py::test_expiry_stops_the_delegate_but_not_the_review_of_what_it_sent_in_time`.
This is a documented limit, not a pass.

## How GAP tests are checked

GAP tests that claim "nothing calls X" are structural: `test/e2e/support/codeGraph.ts` parses Pi's TypeScript with the
TypeScript compiler API, computes the modules reachable from the product entry points (`package.json` `pi.extensions`,
`bin`, `extensions/index.ts`) and asks AST questions (call expressions, string literals, the method that encloses a call).
Comments and unrelated substrings cannot satisfy or break them. Only the scan for non-TypeScript files in the S4 bridge check
is a text scan. Behavioural GAPs (plan CLI writes nothing, review verdict never arrives) observe the real plane.

## Caveats

* Child-process environments are listed exactly in `tests/e2e/README.md`. The Pi plan CLI tests set `PI_CODING_AGENT_DIR`,
  `PI_ENGINEERING_STATE_DIR` and `PI_ENGINEERING_HOME` (Pi's own agent-dir and state inputs, pointed at scratch dirs); fake
  `gh` shims are found through `PATH` only; scenario 5's `git` calls inherit the test process environment plus
  `GIT_TERMINAL_PROMPT=0`. Pi test processes themselves inherit the caller's environment.
* Temp dirs: every Pi e2e temp dir lives under one per-process root removed on exit; the Herdr runner removes its junit dir
  and the throwaway Postgres socket dirs. Directories left in `/tmp` by earlier runs of this suite were not deleted.
* Wording: the first Pi "kill -9" test kills the SERVER (Pi stays up) and is now named so. The new test kills a
  real separate Pi process (`test/e2e/support/piChild.ts`).
* Sibling checkouts: `pi_repo`/`herdr_repo` may be `auto` (discovered by content, same `-tNN` suffix wins). The AutoSpec
  checkout stays an explicit path because several autospec worktrees coexist.
* asyncpg must be installed for S6 (README prerequisites). Pi `biome check .` has pre-existing format failures in
  unrelated test files (e.g. `test/unit/attach-inspector.test.ts`); T16 files are clean.
* No flake campaign beyond the runs reported here; the Herdr regression has a known flaky telemetry test.
* Local-only: none of this ran in CI; Woodpecker integration of `run-acceptance.sh` is not part of this change.
