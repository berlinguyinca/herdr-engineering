# Validation and integration handoff

2026-10-30, isolated branch `feat/event-coordinator`, base `fabc61c`.

- `PYTHONPATH=. pytest -q tests/test_event_coordinator.py tests/test_artifacts.py tests/test_contracts.py`:
  49 passed, including 38 event scenarios. Tests were observed failing before domain
  implementation and again before browser/fixture additions, then passing.
- Existing Ruff executable from `chem-evidence/.venv/bin/ruff check` on both new
  modules and event tests: passed. No package was installed or dependency changed.
- `python3 -m compileall -q` on both new modules: passed.
- `git diff --check`: passed; original Herdr Engineering checkout remains clean.
- Offline CLI published a 38,939-byte report through existing artifact workspace.
  SHA256 `fbee91b6d93604f846768ceb9a427000dbb3c1107fb3d979406d89bb2a19122e`.
  Report path:
  `/Users/wohlgemuth/IdeaProjects/.worktrees/herdr-event-artifacts/event-coordinator/task-event-draft-001/art_7987fac60f0c41d786c5/report.json`.
  This report is a local fixture artifact, not browser or venue evidence.
- Canonical browser schema required fields, constants/enums, scope reference patterns
  and unbound invariants were checked against emitted envelopes. A general JSON Schema
  validator was unavailable; none was installed. Browser binding remains unbound.
- Full suite collected 3,533 tests. Run was interrupted after 469 passes and 1 failure
  in 151 seconds, so the full suite is NOT green or complete. Failure:
  `tests/control_plane/test_ci_api.py::test_telemetry_auth_limits_invalid_bundles_and_performance`,
  oversized telemetry request resets connection instead of returning HTTP 400.
  Reproduced independently on untouched main `fabc61c`: 1 failed in 0.61 seconds,
  Python 3.14.4 on macOS. No event module is imported by that test. Left unchanged.

## Production blockers

1. Authenticated PA specialist registration, task queue and safe-point/model/budget
   lifecycle contracts are not present in this domain implementation.
2. Authenticated grant issuer/revocation and transactional outbox/delivery reconciliation
   must be supplied by platform/email owner. Local Grant values are NOT authority.
3. Authorized email/inbox and Sheets adapters do not exist here; spreadsheet is a
   user-summary fixture. No live mail, form, sheet write or venue research was attempted.
4. Browser contract v1 at e47b9f0 is intended only and unbound for event tasks.
5. Date/time/timezone, adult and child counts, budget/currency are missing. Official
   identities and recipient ownership require fresh verification before live use.
6. Phone push and completion/attention integration belongs to PA platform; report
   generation works locally but does not assert a deployed persistent agent.

Operational snapshots must be private and reconciled with current central action history
and revocations. Do not restore old receipts/grants as new authority or retry uncertain
sends. No outbound method or live account configuration is included in this branch.

## Independent review fixes

The reviewer requested immutable selected task scope, redaction of personal sheet
metadata in the committable fixture, unambiguous reply matching, explicit grant
channel binding and clarification of snapshot authority limits. Four added regression
tests failed before fixes and all 49 focused tests passed afterward. Scope is now
checkpointed and enforced across venue/evidence/receipt/reply/browser paths. Grants
require a channel and proposals support email only. A second review was requested.

Second review confirmed the five prior findings resolved, then identified mutable
Grant hash input. Six malformed/mutable/duplicate hash regression cases failed before
adding strict immutable unique SHA-256 tuple validation; all 49 focused tests passed.

Final independent re-review confirmed the hash finding resolved and reran all 49
focused tests, Ruff and compilation successfully; no further code changes requested.
The reviewer's Python LSP backend was unavailable (transport closed), so no LSP/type
analysis result or formal tool approval is claimed. Static validation here is Ruff
plus Python compilation; there is no configured new dependency or typecheck setup.
