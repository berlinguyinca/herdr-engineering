# Verification evidence

## Resumed work — 2026-10-28 (current evidence)

**Current implementation lane:** the newer upstream-based admission port is in
`/home/wohlgemuth/IdeaProjects/.worktrees/scheduler-checkpoint-safety-20261028-LMK3Bn`,
branch `codex/scheduler-admission-20261028-829e6b`, based on `d8cfceb`.
The shared original checkout remains preserved but does not contain the latest
lease, legacy-context, historical-policy, and general transaction rollback
fixes. Continue implementation and review in the isolated delivery worktree;
do not copy the older API/repo wholesale over current upstream code.

Draft upstream delivery:
https://github.com/berlinguyinca/herdr-engineering/pull/322, containing commits
`c168804` (shared fixture), `71427d3` (durable authority), and `bda7961`
(flagged API/fleet reporting/UI counter integration). Final targeted Python
selection: **297 passed in 61.28s**. Node counter tests: **three passed**.
Playwright scheduling preview: **one passed**, preserving edited forms while
refreshing and reporting progress/errors. The current upstream view's existing
visibility-aware polling/morphing is retained rather than replaced by the older
standalone renderer in this shared checkout.

Completed follow-up: `aa3ad31` fixes the deterministic CI-history test race;
`56cf2b3` applies the existing same-origin guard to scheduler POSTs. Fresh broad
rerun: **4,947 passed, 12 skipped, one deselected, two xfailed** in 1,110.72s.
The late same-origin delta separately passed **46 HTTP/API checks**. All modified
Python files pass Ruff and diff checks; global baseline lint remains outside
the component's green checks.

Independent P01 safety port:
https://github.com/berlinguyinca/herdr-engineering/pull/327, commit `ce7e537`.
Only the current runner's parent preference and two regression tests changed.
Parent links now pass through fresh host opt-out filtering; opting out before
or during a request reroutes later children while preserving lineage and the
existing parent pane. **40 collab-spawn tests passed**, with lint/diff checks and
independent review. This port is separate from PR #322 and does not enable flags.

Current P04 prerequisite worktree:
`/home/wohlgemuth/IdeaProjects/.worktrees/scheduler-parent-policy-20261028-d0f22dd8`
now uses branch `codex/scheduler-host-lease-20261028-d94f7aa5`, based on
`56cf2b3`; the P01 branch remains preserved remotely. Host-bound renewal derives
identity from the authenticated observation and a matching claimed reservation.
It uses fixed TTL 90 and returns no credentials. New service cases passed
**51 tests**, service plus existing scheduler/actual PostgreSQL selection passed
**119**, and new HTTP/service checks passed **62**. Malformed HTTP proof types
use 400; invalid authority uses generic 403. Review and broader callback checks
are in progress. No actual process lifecycle integration or migration is claimed.

The initial broader upstream run completed: **4,945 passed, 12 skipped, two xfailed,
one deselected, one failed**, in 1,116.64s. The CI-history failure expected
exhausted coverage while the first GET starts asynchronous head revalidation.
It passed alone and the entire CI API file passed **96 tests in 48.64s**.
A deterministic synchronization correction was then implemented and reviewed;
the successful rerun is recorded above. Snapshot/suppression deltas have dedicated post-fix
checks rather than being attributed to the earlier-started broad run.

Fresh original-checkout post-change results: **126 passed in 23.90s** across
scheduler/domain/resource/ingest/native HTTP selections; **68 passed in 2.18s**
across repo/CI-debug/review transport checks. The mixed-directory fixture
reproduction passed **33 tests in 2.68s** after relocation. Full original
control-plane baseline: **1,441 passed, five skipped** in 442.80s.

Upstream-based admission port:

- Preserves newer V2 envelopes, `EventAlreadyStored`, ledger/federation handling,
  host metric projections, and API `_resource_principal()` ownership.
- Carries scheduler domain constraints, durable cross-process admission,
  token-bound observations, queue fairness/limits, and lease serialization.
  Native launch, scope supervision, simulator, and recovery remain separate
  integration increments; the isolated port intentionally does not advertise
  those local-only methods as released features.
- Three integration blockers were reproduced and addressed: generic ingest
  granting scheduler authority; cross-replica release leaving a renewable local
  lease; active legacy rows without owner/workspace constraint context.
- Historical client `HostPolicyChanged` envelopes are filtered before SQL
  latest-row selection and during replay. Poisoned snapshots are disabled;
  claims reject missing, expired, fenced, mismatched, or stale-token leases.
- Eleven failing general COMMIT regressions now pass: no failed local policy
  enable, reservation/lease insert/update, stream receipt, or subscriber event.
  Receipts are appended/published after commit, including correct same-stream
  sequencing; backups cannot read an in-flight scheduler transaction.
- Fresh focused upstream results before the final general rollback increment:
  **268 passed in 63.46s** (scheduler/API/ingest/V2/events/sink), **55 passed in
  22.90s** (fleet agent/probe), **45 HTTP/API checks**, and **102 ingest/domain/
  service checks**. The final rollback/sink/actual-PostgreSQL selection passed
  **36 tests in 30.38s**; focused scheduler/rollback/authority selection passed
  **96 tests in 0.61s**. All changed-file Ruff and diff checks pass.
- At that checkpoint the upstream-based control-plane plus fleet/resource/domain
  run was still running; its failed attempt and successful corrected rerun are
  recorded above. New production admission remains
  off; live legacy Mac aliases were freshly read back with `auto_agents=false`.

Latest telemetry persistence increment (after the full runs below):

- Reproduced two missing guarantees: event-only restart lost host capacity and
  failed persistence still changed capacity. Both now have passing regressions.
- Added token-bound, acknowledged `scheduler_observation` events, authoritative
  admission refresh across replicas, event-only reconstruction, policy-revision
  preservation, and a generic-ingest authority exclusion.
- Reproduced a stale dashboard on a second replica; dashboard reads now refresh
  observations, policies, and reservation history under the admission lock.
- Fresh combined resource/service/persistence/locking/ingest/PostgreSQL run:
  **86 passed in 17.69s**, including six actual PostgreSQL cases. Additional
  API/sink/native-launch/locking/policy selection: **60 passed in 23.32s**.
- Targeted Ruff and `git diff --check` passed. This increment is in the original
  shared checkout, pending separate upstream integration; the three existing
  draft PRs below do not contain it. Native migration remains disabled.

P02/P03 follow-up review and verification:

- Independent review reproduced three additional defects. Added failing tests
  for transaction COMMIT failure, a normal heartbeat poisoning the resource
  sequence check, and a later host disable disappearing on event-only restore.
- Resource observations now have a separate authenticated watermark and supply
  the scheduler's capture timestamp even when ordinary heartbeats arrive.
  Observation projection waits until transaction commit; rollback also removes
  the aborted local observation event. Replay restores the highest operator
  policy revision independently of stream ordering.
- Dashboard freshness advances with elapsed time. Execution claims recheck
  current pressure/capacity after commit using the same capacity validation as
  reservation bind; a failed recheck does not claim execution.
- Fresh combined domain/resource/service/persistence/locking/ingest/PostgreSQL
  result: **98 passed in 17.73s**, including seven actual PostgreSQL cases.
  Native HTTP integration separately: **five passed in 2.59s**. Mixed-directory
  argument ordering later reproduced missing `acknowledged_sink` resolution
  even with its file present. The initial inference of a transient missing file
  was incorrect. See the fixture-scope correction below.
- Changed implementation files for this increment: `control_plane/repo.py`,
  `control_plane/sink.py`, `control_plane/scheduler_service.py`,
  `control_plane/ingest.py`, and `scheduler.py` under `herdr_engineering/`.
  Regression files: `tests/test_resource_probe.py`,
  `tests/control_plane/test_scheduler_postgres.py`,
  `tests/control_plane/test_scheduler_service.py`, and
  `tests/control_plane/test_ingest.py`. Operations notes updated in
  `docs/operations/fabric-scheduler.md`.
- Architecture: reuse the existing event store and admission advisory lock;
  no second telemetry service or model/GPU scheduler. No schema migration is
  needed for the new event entity type. Existing historical event rows remain
  unchanged. Broader acceptance, upstream integration and fleet canary remain
  outstanding; no production admission or migration flags were enabled.
- A fourth review finding was reproduced against actual PostgreSQL: exporting
  a replica after refresh omitted observations written by a peer. Backups now
  carry the committed authenticated observation map and restore it before
  replaying older local streams. The strengthened PostgreSQL replica test
  passed (4.72s) and also verifies pressure received from another replica blocks
  an execution claim after reservation commit. The fresh API/sink/native/policy/
  bridge selection passed **67 tests in 23.43s**; scheduler view Node tests
  passed **two tests**. Broader runs above are historical for the latest delta.

P06 queue-cap increment:

- Reproduced restart/replica/lifecycle cap bypasses before implementation:
  **18 failing, four passing** regression cases. The final new queue suite has
  **22 passing** cases, including owner/project/global caps, reduced limits,
  idempotent retries, authoritative rows absent from a replica's local view,
  cancellation and admission freeing slots, unrelated scopes, and immediately
  placeable work.
- Changed queue-related code only in `control_plane/scheduler_service.py` and
  added `tests/control_plane/test_scheduler_queue_limits.py`. The service now
  counts authoritative queued rows while holding admission serialization;
  removed redundant `FairQueue`/`_queued_requests` local bookkeeping. Existing
  persisted queues are preserved and may drain even after limits decrease.
- Extended `tests/control_plane/test_scheduler_postgres.py`: 20 requests from
  four separate processes with owner queue cap three yield exactly three
  queued and 17 rejected. New queue suite plus this actual contention case:
  **23 passed in 3.28s**. Executor's scheduler/domain/simulator/workspace suite:
  **97 passed in 21.49s**. Targeted lint and diff checks passed.
- No schema change, no production flag change, no migration enablement. The
  broader control-plane regression was started before this queue edit and is
  a baseline for the telemetry increment; its result cannot substitute for
  the queue-specific post-change checks. Upstream integration remains open.

Test harness correction:

- The complete `tests/control_plane` run passed **1,441 tests, five skipped**
  in 442.80s, with one intentionally weak JWT-fixture key warning. It started
  before the queue edit; post-change targeted checks remain necessary.
- A combined post-change selection reached **121 passed, five setup errors**:
  only native HTTP tests could not resolve `acknowledged_sink`. Isolated native
  tests passed. Reduced deterministic reproduction:
  `pytest tests/control_plane/test_scheduler_queue_limits.py tests/test_scheduler.py tests/control_plane/test_scheduler_native_integration.py`
  yielded **28 passed, five setup errors** under importlib collection.
- Moved the unchanged acknowledged sink fixture from the nested
  `tests/control_plane/conftest.py` into shared `tests/conftest.py`, alongside
  the existing shared host-configuration fixtures. This repairs scope across
  mixed domain/control-plane selections without duplicating a sink or changing
  its acknowledgment semantics. The empty nested fixture file was removed.

- Complete Python run: **2,383 passed, 8 skipped, one test-fixture warning** in
  619.68 seconds. Log: `/tmp/herdr-scheduler-final-RjuhAoOQ.log`.
- Complete `npm test`: **286 passed** in 164.13 seconds.
  Log: `/tmp/herdr-scheduler-ui-NXsIP7iH.log`.
- The two preceding full Python runs exposed an edge test inheriting live host
  configuration and then a strict XPASS for a fixed secret-redaction defect.
  The edge subprocess is now isolated; the redaction assertions are normal,
  strengthened regression tests rather than removed or loosened assertions.
- Additional post-run redaction/reference/locking/native-launch/recovery checks
  passed separately (68 and 73 test selections). The broader run above preceded
  the final shared-reference and lock-order additions; do not represent it as
  covering those newer cases. Their dedicated regressions and real DB/scope
  checks are recorded in `release-safety-audit.md`.
- All five actual PostgreSQL admission/recovery tests passed in 17.13 seconds.
  Four real/unit Linux scope tests passed in 0.55 seconds.
- Independent bounded review approved checkpoint/scope and the final event
  redaction patch after identified findings were reproduced and corrected.
- Repository `scripts/validate.sh` still stops at **54 global Ruff errors**.
  Log: `/tmp/herdr-scheduler-validate-NgBExNzZ.log`. Targeted scheduler/security
  lint and diff checks pass; the complete script is NOT green.
- Strict dependency doctor: handoff CLI/skills **ok**, overall **warn**.

Scoped upstream-based delivery (draft, not merged):

- https://github.com/berlinguyinca/herdr-engineering/pull/305 — immutable private
  checkpoint storage; commit `076a2b6`; 22 upstream-based checks passed.
- https://github.com/berlinguyinca/herdr-engineering/pull/307 — verified Linux
  scopes; commit `d5ae356`; five upstream-based/live/script checks passed.
- https://github.com/berlinguyinca/herdr-engineering/pull/308 — event credential
  redaction including pagination/JSON/shared references; commit `4cc6060`;
  35 upstream-based security/reference checks passed.

The isolated delivery worktree is
`/home/wohlgemuth/IdeaProjects/.worktrees/scheduler-checkpoint-safety-20261028-LMK3Bn`.
It is clean and currently on the event-redaction branch. The shared original
checkout and concurrent work remain intact. These PRs are component fixes, not
full P00–P10 acceptance; issue #290 remains open and migration remains disabled.

## Environment

- Host: `bender`, Linux, 2026-10-25.
- Python: `.venv/bin/python` (Python 3.12 environment).
- Archive hash: `85c84d7f72c9e668aca47e0196a8ce9af75cefa87bc13a44403b4d1d9beae39c`.

## Fresh commands

| Command | Result |
| --- | --- |
| `./.venv/bin/python -m pytest -q tests/test_host_policy.py tests/test_telemetry.py tests/test_scheduler.py tests/test_workspace_quotas_recovery_replay.py tests/control_plane/test_host_scheduler_policy.py tests/control_plane/test_scheduler_service.py tests/control_plane/test_api.py::test_scheduler_api_is_readable_and_queues_untrusted_host` | 23 passed |
| `./.venv/bin/ruff check <scheduler changed files>` | All checks passed |
| `./.venv/bin/python -m py_compile deploy/control-plane/collab-spawn.py` | exit 0 |
| `node --test packages/scheduler-view.test.js` | 1 passed |
| `./.venv/bin/python -m pytest -q tests/control_plane/test_collab_spawn.py tests/control_plane/test_collab_join_script.py` | 45 passed after rollout-gate compatibility fix |
| Combined scheduler + collaboration regression command (all new tests plus API/spawn/join suites) | 68 passed in 168.10s |
| `npm test` | 267 passed, 0 failed |
| `./.venv/bin/python -m pytest -q --maxfail=1` | stopped after bounded runtime at ~20% while long integration suite was still running; before that run exposed the scheduler API route being overwritten by concurrent `api.py` edits, which was restored and revalidated by the 68-test command |

The repository-wide Ruff command is not a clean baseline because it reports pre-existing style violations in `agent_mine.py`, `cli.py`, `fleet.py`, `fleet_config_cli.py`, and `web.py`; the scheduler-touched file set is clean.

## Ralph continuation verification — 2026-10-25

- `./.venv/bin/python -m pytest -q tests/test_host_policy.py tests/test_telemetry.py tests/test_scheduler.py tests/test_workspace_quotas_recovery_replay.py tests/control_plane/test_host_scheduler_policy.py tests/control_plane/test_scheduler_service.py tests/control_plane/test_api.py::test_scheduler_api_is_readable_and_queues_untrusted_host tests/control_plane/test_collab_spawn.py tests/control_plane/test_collab_join_script.py` → **68 passed in 56.74s**.
- `node --test packages/scheduler-view.test.js` → **1 passed**.
- Targeted Ruff, `py_compile`, and `git diff --check` → **clean**.

These are fresh post-Ralph-loop results against the current worktree. The repository-wide suite remains a known bounded validation gap because its long integration run was stopped before completion; no production rollout or migration flag was enabled.

## Persistence continuation verification — 2026-10-26

- `./.venv/bin/python -m pytest -q tests/control_plane/test_scheduler_persistence.py tests/test_scheduler.py tests/control_plane/test_scheduler_service.py` → **9 passed in 0.35s**.
- Targeted Ruff, Python compilation, and `git diff --check` for the persistence slice → **clean**.
- Fresh behavior covered: export/import restoration, event-only reservation replay, idempotent restart retry, commit after restore, and persisted reservation expiry.

## Lease continuation verification — 2026-10-26

- `./.venv/bin/python -m pytest -q tests/control_plane/test_scheduler_persistence.py tests/test_scheduler.py tests/control_plane/test_scheduler_service.py` → **10 passed in 0.08s**.
- Targeted Ruff, Python compilation, and `git diff --check` for scheduler/repository/service lease changes → **clean**.
- Fresh behavior covered: one persisted execution lease per committed reservation, idempotent commit retry, lease state persistence on release, and lease restoration through repo export/import.

## Lease heartbeat verification — 2026-10-26

- `./.venv/bin/python -m pytest -q tests/control_plane/test_scheduler_persistence.py tests/control_plane/test_scheduler_service.py tests/control_plane/test_api.py::test_scheduler_lease_heartbeat_route_renews_and_rejects_stale_token tests/test_scheduler.py` → **13 passed in 0.64s**.
- Targeted Ruff, Python compilation, and `git diff --check` → **clean**.
- Fresh behavior covered: authenticated lease renewal, stale token/generation rejection, API transport, expiry fencing, and durable lease state.

## Known validation gaps

- No real 2–3 host canary was performed from this local change; no deployment flags were enabled.
- Full adapter coverage for Pi/Claude/Codex/AutoSpec/Plannator and dashboard Playwright screenshots remains incomplete.
- Durable restart hydration/reconciliation for scheduler reservations/leases remains incomplete.
- Live migration is intentionally not enabled or claimed safe.

## Checkpoint/resume/fault continuation verification — 2026-10-26

- `./.venv/bin/python -m pytest -q tests/test_checkpoint_resume.py tests/test_workspace_quotas_recovery_replay.py tests/test_scheduler.py` → **12 passed in 0.18s**.
- Targeted Ruff, Python compilation, and `git diff --check` for checkpoint/resume/recovery changes → **clean**.
- Fresh behavior covered: private atomic checkpoint writes, owner/digest verification, uncertified native-family defaults, certified-contract gating, old-writer fencing, and the complete specified ambiguous-failure inventory.

## Adapter/workspace/quota/UI continuation verification — 2026-10-26

- `./.venv/bin/python -m pytest -q tests/test_workspace_quotas_recovery_replay.py tests/test_adapter_registry.py tests/control_plane/test_scheduler_service.py` → **11 passed**.
- `node --test packages/scheduler-view.test.js` → **1 passed**.
- Targeted Ruff, Python compilation, and `git diff --check` → **clean**.
- Fresh behavior covered: family registry admission, reservation release on native launch failure, manifest conflict/preflight gates, owner queue caps, and read-only placement preview rendering.
- Additional native bridge evidence: `native_machine_launcher` resolves saved Herdr machines and validates argv before forwarding; adapter/bridge suite now reports **12 passed**.

## Integration/canary continuation verification — 2026-10-26

- `npm run test:ui` → **60 passed, 1 failed** in the full 61-test suite. The single failure was the existing timeout-sensitive mission-history test; rerunning `node --test --test-name-pattern='recorded history requests time out and retry' tests/playwright/ui.test.mjs` passed in **0.77s**, so the failure is suite-contention/flakiness rather than deterministic scheduler UI behavior.
- `./.venv/bin/python -m pytest -q tests/test_herdr_adapter.py tests/control_plane/test_collab_spawn.py tests/control_plane/test_collab_join_script.py` → **57 passed in 37.69s**.
- Read-only host probes: `beast` reachable as Linux but has no `herdr` binary on PATH; `macbook-pro` reachable as `mac.lan`, macOS 26.5.2, and has `/Users/wohlgemuth/.local/bin/herdr`.
- Canary status: blocked from a real 2–3 host scheduler canary until the Linux destination is provisioned with Herdr and a certified runtime resume contract. No scheduler admission/migration flags were enabled.
