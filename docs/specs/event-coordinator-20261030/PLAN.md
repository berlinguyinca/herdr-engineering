# Event coordinator implementation plan

**Goal:** reusable offline venue research/correspondence domain, with production
execution delegated to authorized PA/Pi and capability owners.
**Architecture:** immutable user/task-bound domain records; pure draft and policy
functions; checkpoint projections; existing artifact publisher; unbound browser v1.
**Stack:** existing Python standard library, pytest, existing artifact contracts.
**Spec:** `SPEC.md` in this directory.

Constraints: no new dependencies; no external outreach; no shared-browser edits;
no new personal-assistant runtime, model router, scheduler or central event store.

- [x] Locate canonical repository and confirm runtime ownership with main session.
- [x] Isolate branch/worktree at fabc61c and ground spec in existing code.
- [x] Add birthday fixture retaining unknown requirements and identity caveats.
- [x] Observe failing tests before domain implementation.
- [x] Implement draft, policy, receipt, evidence and checkpoint contracts.
- [x] Observe failing browser/fixture tests before adding local envelope/artifact flow.
- [x] Pin browser v1 intended boundary at e47b9f0; preserve unbound semantics.
- [x] Run 49 focused tests, Ruff, compilation and local artifact demo.
- [x] Attempt full suite, reproduce CI telemetry failure on untouched baseline.
- [ ] Integrate production PA/Pi/email/Sheets/browser capabilities: blocked on owners'
  authenticated interfaces listed in VALIDATION.md; deliberately outside local slice.

Independent code review and final local checkpoint are recorded in VALIDATION.md.
No production deploy, PR, remote push or outreach is part of this plan.
