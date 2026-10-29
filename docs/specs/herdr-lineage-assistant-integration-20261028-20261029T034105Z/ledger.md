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
| S0 assistant hardening | **partial** (shell allowlist fail-closed done; connector-route grant bypass, action TTL/principal-bound hash, dry-run, tenant model not done) | `test_assistant_service.py::test_run_shell_denied_when_allowlist_empty`; all 162 `-k assistant` tests passed |
| S1 `read_lineage` tool | **done at unit/service level; independent review done, 1 medium + 2 low fixed (malformed depth/limit, clamping, grant-denial test)**; not exercised through the live HTTP chat route or the UI | new `control_plane/assistant_lineage.py`; `tests/control_plane/test_assistant_lineage.py` (11 tests); wired in `api.py` beside `intelligence_read`; `-k 'assistant or graph or intel_assistant or api'` = 736 passed (4 min). Flag stays default-off; no grants by default |
| S2 artifact-to-graph | **done for the HTTP commit route; partial overall** | new `control_plane/artifact_lineage.py`; `tests/control_plane/test_artifact_lineage.py` (11 tests incl. real HTTP route and a mutation check that the route test fails without the wiring). Commons `artifact:<id>` node with immutable version facts; `produced` edge best-effort. **Limits:** scope is `private` (only the committer sees it; not even operators), so team visibility needs an ACL-to-scope mapping; the render-runner commit path (`artifact_render_runner.py:111`) does not emit; no UI link yet |
| S3-S4 Pi continuity | **partial**, PR berlinguyinca/pi-engineering#460 (worktree `~/IdeaProjects/pi-lineage-continuity-wt`, base `6082fb9`) | **Done:** rollover records a `handoff` edge; observed rollover acknowledges open checkpoint-bearing handoff records; checkpoints carry bounded handoff context; 10-rollover chain test; 882 tests in 57 focused files, 0 failures; independent review, 6 of 7 findings fixed. **Not done:** broker does not populate the new checkpoint fields; no context-threshold trigger (phase handoff still off without `phaseHandoff`); no verify-before-revoke ordering; no runaway-process termination; no crash-at-each-stage or cross-host tests; full `npm test` not completed |
| S5 UI | **partial**, PR stacked on #365 (branch `feat/workbench-ui-20261029`) | inspector measured facts, bounded pan/zoom viewport, status strip, live/replay badge, operator-dashboard terminal link; `ui-gates-workbench.test.mjs` 16 tests, `collab-lineage-viewport.test.js` 21 tests, baselines 100/100, evidence in `evidence/screenshots/`. **Not done:** filters are not behind a disclosure; no graph minimap; terminal uses the dashboard session page only, not `authorize_workspace`; Missions page baselines; assistant tab baselines |
| S6 room TUI, request adapters | not started | |
| S7 CI repair loop | not started | |
| S8 fault matrix, metrics, runbooks | not started | |

## Blockers and open items
- None blocking. Postgres-dependent e2e tests need a local Postgres.
- S2 visibility is the committer only until artifact ACLs are mapped to graph scopes; the assistant cannot yet read other users' artifact nodes.
- Pi side has no CI workflow files on `origin/main`; CI status there is unverified.
- Correction to audit.md: the 0.7/0.75/0.8 constants are not all competing rollover triggers (0.7 is a worker budget share, 0.8 a UI warning); only 0.75 is the phase-handoff trigger, and it is off by default.
- Supervisor replacement was not assessed in the Pi audit.
- Open decision, not made on the user's behalf: whether the proposed 70/80/90 context thresholds become defaults (the spec labels them proposals).
