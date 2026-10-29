# Implementation ledger

Resume point for future agents. Update after every phase. Statuses are pass / partial / blocked / not started, with evidence.

## Run context
- Run id `20261029T034105Z`; execution host bender; branch `feat/lineage-assistant-20261029T034105Z` off `herdr-engineering` `origin/main` `694aba1`; worktree `~/IdeaProjects/herdr-lineage-assistant-wt`.
- Package ZIP copied from the Mac (`~/Downloads`, 10,253 bytes) over SSH; sha256 `7db18708377c59a7283eb0ecab9db90976c3d9912a11e120508d80ddbe1252cc`. Original files preserved in `original-package/`.

## Phases
| Phase | Status | Evidence |
|---|---|---|
| P0 audit | **done** (code reading only; no tests run) | `audit.md`, `reuse-map.md`, `rakazo-fit-gap.md`, `architecture.md`, `test-matrix.md` |
| S0 assistant hardening | not started | |
| S1 `read_lineage` tool | not started | |
| S2 artifact-to-graph | not started | |
| S3-S4 Pi continuity | not started (needs a pi-engineering worktree off `origin/main` `6082fb9`) | |
| S5 UI | not started | |
| S6 room TUI, request adapters | not started | |
| S7 CI repair loop | not started | |
| S8 fault matrix, metrics, runbooks | not started | |

## Blockers and open items
- None blocking. Postgres-dependent e2e tests need a local Postgres.
- Pi side has no CI workflow files on `origin/main`; CI status there is unverified.
- Supervisor replacement was not assessed in the Pi audit.
- Open decision, not made on the user's behalf: whether the proposed 70/80/90 context thresholds become defaults (the spec labels them proposals).
