# Shared Supervision Dashboard Implementation Plan

**Goal:** Display the same durable room/workspace, tab/job and agent tree across
Collaboration, Agents and Personal Assistants using existing collaboration identities.
**Architecture:** Explicit durable job-room/workspace links; authorized projection
joins stable collaboration IDs to the existing supervision event stream. One reusable
collapsible component renders the projection in existing dashboard surfaces. Existing
tasks, grants and runtime execution remain authoritative and unchanged.
**Tech stack:** Python standard library, existing event sink, vanilla JavaScript,
Node test, pytest and existing Playwright.
**Spec:** `2026-10-30-tab-supervision-design.md`, updated by the agreed shared hierarchy.

## Constraints

No dependencies, live launches, merges, pushes, deployment or edits in another
lane's worktree. Coordinate collab/API boundary with root. Shared IDs only; unknown
p5 parentage must not become an inferred edge. Room access does not grant terminal
access. No new runtime, task store or assistant execution implementation.

- [x] Add durable explicit job/room/workspace association and replay tests.
- [x] Add pure authorized projection with exact agent joins, room/owner isolation,
  hidden-parent handling, current grants and safe operational metadata.
- [x] Wire projection through existing collaboration authorization and event sink;
  preserve long-poll updates and private-event boundaries.
- [x] Add shared accessible tree with job and branch collapse, search, details,
  reviewer role, status/health, budget/checkpoint and evidence references.
- [x] Mount in Collaboration Flow, Agents and Assistants using the same endpoint
  and IDs; retain legacy task graph as a separate relationship view.
- [x] Add backend, renderer, route and browser regressions for matching identity,
  isolation, restart, unknown parentage, refresh/focus and mobile containment.
- [x] Run independent review, relevant Python/JS/browser suites and static checks;
  document integration limits and report a reviewable commit to root.


## Validation evidence and limits

- Latest focused/adjacent Python run: 238 passed.
- Full client/component Node suite: 369 passed.
- Shared tree/catalog browser suite: 8 passed; actual Agents, Personal Assistants
  and Collaboration routes reuse identical fixture agent IDs.
- Updated existing Collaboration composer browser regression: passed; it explicitly
  opens the separate legacy task-graph disclosure before preserving all assertions.
- Broader collaboration Python sweep: 1,195 passed, 49 skipped, one existing helper
  runner failure in `test_the_egress_proxy_is_stopped_even_when_its_supervisor_is_gone`.
  The same `proc_stat(pid) is None` failure reproduces on unchanged canonical main
  `fabc61c` on this macOS host. No helper-runner code was changed.
- Broad historical UI checks also include baseline composer-indentation/density
  keyboard failures and hard-coded Linux browser paths; the new browser suite uses
  the installed Chrome executable and is green. These unrelated tests were not fixed.
- Ruff configured checks, Python compilation, JavaScript syntax and diff whitespace
  checks passed. Independent review approved the bounded policy/UI/API checkpoint.

Screenshots and limitations are described in `docs/evidence/tab-supervision/README.md`.
Production runtime hooks, deployment and authoritative multi-process/database rollout
remain root integration responsibilities. No live pane topology was migrated.
