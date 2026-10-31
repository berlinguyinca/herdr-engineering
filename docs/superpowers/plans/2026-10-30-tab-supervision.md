# Tab Supervision Implementation Plan

**Goal:** Add the approved durable supervision policy seam without launching workloads.
**Architecture:** Serialized commands persist to the existing required sink before
applying an event projection. Authentication and runtime execution remain with the
existing operator/collaboration boundary.
**Tech stack:** Python standard library; pytest; existing DurableEventSink.
**Spec:** `docs/superpowers/specs/2026-10-30-tab-supervision-design.md`

## Constraints

Only add `herdr_engineering/control_plane/tab_supervision.py`, its focused tests,
and these documents in `feat/tab-supervision`. No dependencies, shared API edits,
root worktree writes, merge, push, deployment, or live lifecycle actions.

## Task 1: Durable job tree and narrowed policy

- [x] Create `tests/control_plane/test_tab_supervision.py` against `Supervision`,
  `Actor`, `Policy`, and `Binding`; run pytest to prove the feature is absent.
- [x] Implement `create_job(actor, job_id, tab_id, agent_id, task_id, policy)` and
  `delegate(actor, child_id, task_id, policy)`. Actor carries current scope/model
  grants; creation checks ownership, current grants and persistent ancestor limits.
- [x] Implement persist-before-apply using `record_required`, detached state views,
  unique tab/job and agent identities, and contiguous validated replay.
- [x] Verify narrowing, denied cross-user commands, failure atomicity and restart.

## Task 2: Explicit bindings, progress and cancellation

- [x] Add failing tests for `bind`, `checkpoint`, `charge`, `health`, `cancel`,
  `conclude`. Test pane loss and self-reported done cannot complete work.
- [x] Implement immutable parentage, explicit internal/visible bindings, deduplicated
  usage receipts and atomic ancestor charging. Scope receipts by job+receipt ID.
- [x] Implement subtree stop intents and evidence-gated parent integration.
- [x] Verify replay retains cancellation and evidence; no lifecycle calls occur.

## Task 3: Reviewable checkpoint

- [x] Run focused pytest and relevant existing helper/adapter tests.
- [x] Run ruff and Python compilation on changed Python files; inspect diff.
- [x] Report exact implemented seam and deferred integration to root, with paths,
  verification results and stop condition. Preserve worktree for root review.

## Checkpoint delivery

Added the module, focused behavioral/security tests, and design/plan only.
The independent reviewer identified real-clock replay and binding reservation
issues; regression tests now cover canonical timestamps, terminal/historical
runtime reservations, delayed binding, queued verification refusal and identical
binding receipt idempotency. No dependencies were added. Existing required sink,
collaboration task/grant semantics and native adapter remain integration sources.

No standalone type-check command exists for this Python project. Validation uses
pytest, Ruff's configured static rules and compileall. Root integration, live
adapter enforcement, authoritative database replay and multi-process operator
serialization are not exercised by this policy-only checkpoint.
