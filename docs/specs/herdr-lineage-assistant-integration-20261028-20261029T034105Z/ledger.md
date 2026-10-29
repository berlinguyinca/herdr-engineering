# Implementation ledger

Resume point for future agents. Update after every phase. Statuses are pass / partial / blocked / not started, with evidence.

## Run context
- Run id `20261029T034105Z`; execution host bender; branch `feat/lineage-assistant-20261029T034105Z` off `herdr-engineering` `origin/main` `694aba1`; worktree `~/IdeaProjects/herdr-lineage-assistant-wt`.
- Package ZIP copied from the Mac (`~/Downloads`, 10,253 bytes) over SSH; sha256 `7db18708377c59a7283eb0ecab9db90976c3d9912a11e120508d80ddbe1252cc`. Original files preserved in `original-package/`.

## Pull requests
- berlinguyinca/herdr-engineering#365 (audit docs, read_lineage, shell fail-closed, artifact-to-graph). Not merged; CI not yet reported.
- berlinguyinca/pi-engineering#460 (Pi continuity slice). Not merged; no CI workflow files exist on that repo's main.

## Phases
| Phase | Status | Evidence |
|---|---|---|
| P0 audit | **done** (code reading only; no tests run) | `audit.md`, `reuse-map.md`, `rakazo-fit-gap.md`, `architecture.md`, `test-matrix.md` |
| S0 assistant hardening | **done at unit/service/HTTP level** (branch `feat/assistant-actions-20261029`, stacked on #365) | connector `/request` now goes through the tool gate (`test_assistant_hardening.py`, 38 tests); request hash binds tenant/user/assistant/tool/scope/canonical payload; action TTL; redacted dry-run; approver != requester unless `actions.allow_self_approval`; preauthorized grants need an authenticated operator key and never apply to send_message/run_shell; `read_fleet` scoped by `fleet_context.allowed_principals`; `routines/due` reachable. **Not done:** tenant is still hardcoded `default`; audit log still in-memory/snapshot; the dashboard "test connector" button now gets 400 (needs run_id) |
| S1 `read_lineage` tool | **done at unit/service level; independent review done, 1 medium + 2 low fixed (malformed depth/limit, clamping, grant-denial test)**; not exercised through the live HTTP chat route or the UI | new `control_plane/assistant_lineage.py`; `tests/control_plane/test_assistant_lineage.py` (11 tests); wired in `api.py` beside `intelligence_read`; `-k 'assistant or graph or intel_assistant or api'` = 736 passed (4 min). Flag stays default-off; no grants by default |
| S2 artifact-to-graph | **done for the HTTP commit route; partial overall** | new `control_plane/artifact_lineage.py`; `tests/control_plane/test_artifact_lineage.py` (11 tests incl. real HTTP route and a mutation check that the route test fails without the wiring). Commons `artifact:<id>` node with immutable version facts; `produced` edge best-effort. **Limits:** scope is `private` (only the committer sees it; not even operators), so team visibility needs an ACL-to-scope mapping; the render-runner commit path (`artifact_render_runner.py:111`) does not emit; no UI link yet |
| S3-S4 Pi continuity | **partial**, PR berlinguyinca/pi-engineering#460 (worktree `~/IdeaProjects/pi-lineage-continuity-wt`, base `6082fb9`) | **Done:** rollover records a `handoff` edge; observed rollover acknowledges open checkpoint-bearing handoff records; checkpoints carry bounded handoff context; 10-rollover chain test; 882 tests in 57 focused files, 0 failures; independent review, 6 of 7 findings fixed. **Not done:** broker does not populate the new checkpoint fields; no context-threshold trigger (phase handoff still off without `phaseHandoff`); no verify-before-revoke ordering; no runaway-process termination; no crash-at-each-stage or cross-host tests; full `npm test` not completed |
| S5 UI | not started | |
| S6 room TUI, request adapters | **adapters partial** (assistant side only; no Pi `/room` TUI) | `request_review`, `propose_branch`, `request_mission`, `comment_on_node`, `draft_email` in `assistant.py`; hooks in `assistant_adapters.py`; `test_assistant_actions.py`. Proposals start no work: nothing consumes an approved proposal yet |
| S7 CI repair loop | **partial** | `AssistantService.watch_ci` -> deduped `request_ci_repair` proposal on the existing ActionBoard + dashboard notice; no auto-merge path. Not done: AutoSpec quorum/re-review loop after the repair, exact-SHA tests, board proposals are in-memory with a 30 min window |
| S8 fault matrix, metrics, runbooks | not started | |

## Blockers and open items
- None blocking. Postgres-dependent e2e tests need a local Postgres.
- S2 visibility is the committer only until artifact ACLs are mapped to graph scopes; the assistant cannot yet read other users' artifact nodes.
- Pi side has no CI workflow files on `origin/main`; CI status there is unverified.
- Correction to audit.md: the 0.7/0.75/0.8 constants are not all competing rollover triggers (0.7 is a worker budget share, 0.8 a UI warning); only 0.75 is the phase-handoff trigger, and it is off by default.
- Supervisor replacement was not assessed in the Pi audit.
- Open decision, not made on the user's behalf: whether the proposed 70/80/90 context thresholds become defaults (the spec labels them proposals).
