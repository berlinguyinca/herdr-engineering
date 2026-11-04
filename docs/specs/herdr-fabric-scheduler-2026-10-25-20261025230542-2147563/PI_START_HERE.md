# Continue this handoff in Pi

**2026-10-28 update:** the user resumed Codex in this thread. These transfer
instructions are historical until the user explicitly requests Pi takeover again.
The local Pi package has not been claimed. Read the resume note in PI_HANDOFF.md
before starting parallel work against the shared checkout.

**Latest implementation:** use
`/home/wohlgemuth/IdeaProjects/.worktrees/scheduler-checkpoint-safety-20261028-LMK3Bn`
on `codex/scheduler-admission-20261028-829e6b` for the upstream admission port.
Read the current top of `verification.md` before any takeover. The original
shared checkout lacks the latest lease, historical-policy, and transaction fixes.

## Historical stop and transfer instructions

Codex has stopped implementation and paused its goal. No Pi process was launched.

## Start Pi on bender in the existing checkout

```bash
cd /home/wohlgemuth/IdeaProjects/herdr-engineering
pi
```

Paste this into Pi:

```text
Resume private handoff handoff_26324334401d4fa59e91dfd6033f223a.

First read the entire file:
docs/specs/herdr-fabric-scheduler-2026-10-25-20261025230542-2147563/PI_HANDOFF.md

Use ./.venv/bin/herdr-handoff --root .herdr/handoffs to inspect, claim, and
accept the queued package before starting implementation. Read the original
specification bundle and release-safety-audit.md, then inspect the current
worktree and resume the full scheduler P00-P10 objective. Preserve concurrent
work. Keep uncertified migration disabled. Earlier green focused tests are
not proof of release completion. Finish the remaining integration, fault,
canary, review and delivery gates with current evidence.
```

## Recipient handoff commands

These are for Pi to execute when it begins; Codex has not claimed the package:

```bash
./.venv/bin/herdr-handoff --root .herdr/handoffs inspect handoff_26324334401d4fa59e91dfd6033f223a
./.venv/bin/herdr-handoff --root .herdr/handoffs transition handoff_26324334401d4fa59e91dfd6033f223a claimed --actor pi
./.venv/bin/herdr-handoff --root .herdr/handoffs transition handoff_26324334401d4fa59e91dfd6033f223a accepted --actor pi
./.venv/bin/herdr-handoff --root .herdr/handoffs transition handoff_26324334401d4fa59e91dfd6033f223a in_progress --actor pi
./.venv/bin/herdr-handoff --root .herdr/handoffs resume handoff_26324334401d4fa59e91dfd6033f223a
```

Package:
`.herdr/handoffs/outbox/handoff_26324334401d4fa59e91dfd6033f223a.json`.
It is queued locally, private, and has not been uploaded or published. It refers
to this existing working tree, including uncommitted scheduler work; do not
resume from a clean clone and assume those changes are available there.

If starting from the Mac, use the existing configured SSH route to bender and
start Pi in this checkout there. The Mac clone does not contain this uncommitted
work automatically. Do not copy credentials or publish the handoff.
