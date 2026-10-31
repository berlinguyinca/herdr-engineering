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

Dinner handoff extension: one fixture/schema regression test failed before the new
files existed, then the focused suite passed 50 tests. Ruff passed. Synthetic dinner
runner published report art_af1501cc03ca4eb0a211 (6468 bytes), SHA256
ce9a19e78f1760ee8543d142aafb2bb19e10926deeb478a9a3c4b47ce2b007ea.
Eight synthetic adults, no children, USD400 ceiling; date/time/timezone and venue costs
remain unknown, so no under-budget success or live availability is asserted.

Baseline source diagnosis for main: `control_plane/api.py` telemetry POST branch
(lines 1305-1310 at fabc61c) rejects oversized Content-Length before `_read_json()`.
The failing test's 1,048,576-character padding produces a 1,048,627-byte JSON body.
Observed client failure is ConnectionResetError errno 54 in `HTTPConnection.sendall`,
wrapped by urllib as URLError, before the test receives the expected HTTP 400.
Unread-body rejection while the client is still uploading is the source-grounded
explanation for the reset on this macOS/Python 3.14 environment; no packet trace was
collected. This concerns baseline HTTP rejection behavior, not event fixture code.

Delegation-scope extension: optional keyword user_id/task_id and matching CLI flags
preserve legacy defaults. Two API/CLI regression tests failed before implementation;
52 focused tests passed afterward, including explicit scope propagation through
drafts/checkpoint/browser envelopes/artifact mission and legacy defaults. Report
schema now exposes requirements and missing_requirements, preserving source provenance
and unknown venue cost status. Ruff, compilation and diff checks passed. No browser
imports, grants, runtime orchestration or dependencies were added.

## Contact research and local form preparation extension

New `event_contacts.py` consumes scoped trusted structured observations only; it does
not fetch pages or execute browser operations. Tests first failed because the module
was absent. Four additional duplicate-contact regressions failed before history checks
were added. The focused suite including `tests/test_event_contacts.py` now passes
77 tests (25 contact-preparation scenarios plus the prior 52). Ruff on the new module
and tests, Python compilation and diff checks pass. The previously documented full
suite baseline failure is unchanged; no broad rerun was needed for this pure additive
module. Existing fixture CLI and report schema remain unchanged.

Coverage: scoped identity/resource boundaries, future/expired observations, verified
origin, email-first route deduplication/ambiguity, cross-origin forms, explicit requester
fields, hidden/unsupported required controls, missing date/message values, proposal
hashes, immutable inputs and cross-channel duplicate/opt-out/reply history. All network
addresses are synthetic example.test routes. No live research or external action occurs.

Independent contact-preparation review found query-bearing URLs could retain tokens
in durable provenance/proposals. Source and target regressions failed before the fix;
all URL queries are now rejected at construction, alongside userinfo/fragments.
Operational token-bearing destinations require a future opaque authorized binding.
After the fix, 77 focused tests, Ruff, compilation and diff checks pass. Reviewer
LSP/AST diagnostic transports remain unavailable; no such analysis result is claimed.
Final independent re-review confirmed query source/target rejection and reported no
remaining code/spec/security blocker in the reviewed contact-extension slice. The
reviewer independently reran all 77 focused tests, Ruff, compilation and diff checks.

## Screenshot request regression

The reported UI request (eight adults, ten children, everyone pays their own bill)
was reproduced at the main-lane request parser: parseDinnerRequest returns
INVALID_REQUEST because it only accepts a guest count plus explicit dollar budget.
That rejection occurs before delegation; UI catches it as a generic failed request.
The integration adapter independently restricts children to zero and requires budget.
These files belong to main; root cause and exact reproduction were handed to that
owner, who accepted the bounded parser/adapter/UI fix. They were not edited here.

A new event API characterization test proves eight adults, ten children and null
budget/currency produce a reviewable scoped local draft with explicit missing values,
unknown costs, separate-tabs question still open, and no recipient/outreach. The
focused event/artifact/contracts suite passes 78 tests; Ruff and diff check pass.
This test does not claim the request parser preserves billing preference: that
request interpretation and clarification work remains main-lane ownership.

## Independent UI verification after main-lane fixes

At refreshed http://127.0.0.1:62731, real Chromium in a fresh private context tested:
- `Plan a dinner for eight adults and 10 kids between 6-14 where every adult pays there own tab`
- `Plan dinner for 8 adults and 10 children ages 6 to 14, separate bills please.`

Both reached draft_ready and rendered two venue drafts. Verified adults 8, children
10, guests 18, ages minimum 6/maximum 14, separateBills true and adult bill payers.
Budget/currency remained null; date/timezone/budget/currency stayed missing. The UI
visibly showed children ages 6–14, separate adult tabs, 8 adults + 10 kids and budget
Not specified. Browser stayed unbound, recipients null and quoted costs unknown.
The page explicitly described deterministic extraction and no LLM interpretation.
No page JavaScript errors occurred. Test routing allowed only that loopback origin;
no approval/review button was clicked and no outreach occurred. Prototype source
remained main-owned and was not modified by this lane.

Local evidence (outside source):
`/Users/wohlgemuth/IdeaProjects/.worktrees/herdr-event-artifacts/ui-debug/after-fix.json`
and `latest-request-fixed.png` in the same directory. The fixed PNG was visually
inspected. This proves the two tested requests; it does not establish general
conversational interpretation or live research capabilities.
