# Agent coordination and delivery

## Roles
- Claude planner: inventory repositories, write architecture decision record, partition tiny micro-specs and non-overlapping file ownership.
- Pi local open-weight implementation agents (Qwen/DeepSeek when available): adapters, schema, backend aggregation, persistence, tests.
- Codex: visual design integration, browser UI, terminal map renderer where compatible, responsive accessibility, screenshot comparisons.
- Independent Claude Opus reviewer: architecture, privacy, correctness, UI fidelity, tests; return blocking findings to implementer.
Use available models; never assume model versions, quotas or host availability.

## Workflow
1. Verify current host and working directory, enumerate remotes, uncommitted work and branches. Do not deploy to unrelated hosts.
2. If ZIP originates on Mac Downloads, transfer it to the actual execution host via an authorized SSH/SCP route and verify checksum; do not assume the ZIP is already remote.
3. Extract into unique `docs/specs/herdr-agent-operations-<date-or-hash>/` in the designated integration checkout.
4. Inspect all related repositories and actual frontend before choosing files/interfaces. Record findings and unknowns.
5. Create tiny specs with exact repository/files, owner, tests, acceptance criteria, rollback, dependencies.
6. Spawn parallel agents only with disjoint worktrees/files. Use existing Herdr Rooms, panes, and `/herdr::to*` commands where installed and verified.
7. After each slice run local tests, capture results, independent review, fix blocking issues, rerun. CI optional.
8. Merge only into authorized integration branches; respect branch protections and uncommitted work; never force-push.
9. Capture real before/after screenshots, compare to references, test realtime/replay on web+terminal.
10. Final report: exact commits, screenshots, test evidence, unsupported provider signals, known limitations and rollback.

## Slices
00 discovery + baseline screenshots
01 event schema and provenance
02 provider adapters
03 room messaging + handoff correlation
04 aggregation/replay/auth
05 animated web map
06 native terminal map
07 task/ownership/Work Command Center
08 drift/context/verification
09 cross-host fleet + reconnect
10 accessibility/performance/security + final integration review

## Acceptance
Real Pi, Claude, Codex events visible where APIs permit; live animated map on web and terminal; room-aware communication and distinct task handoffs; stable task ownership; model changes; cross-host and reconnect behavior; replay; private by default; no duplicate collector; no regressions; all local tests; reviewer signoff. Unsupported signals shown unknown.
