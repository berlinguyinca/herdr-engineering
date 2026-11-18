# Verification evidence

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
