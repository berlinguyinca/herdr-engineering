# Phase 4 — integrate every launch path
Create an adapter registry around existing launch mechanisms instead of replacing native Herdr workspace/pane APIs. Each spawn uses `PlacementRequest` and `ExecutionLease`. Reuse `NativeHerdrAdapter` for machine forwarding; refactor `collab-spawn.py` `pick_host` to shared scheduler rather than constructing separate fleet placement logic. Keep existing collaboration auth and sandbox controls; do not let room permissions become command execution capability. Preserve CLI semantics through feature-gated compatibility wrapper.

## Required launch paths
- dashboard create-agent / create-session and remote terminal;
- Pi harness agent + Pi child/subagent, including cross-host parent-linked panes;
- Claude Code and Codex agent creation and delegated children, permitted family-specific launch commands;
- AutoSpec without Plannator and with optional Plannator review, planner/reviewer separated from implementer, issue-linked handoffs;
- collaboration fleet start, A2A-initiated authorized tasks, room work graph;
- resumed/retried sessions and scheduled jobs;
- remote external peers: identity visible for graph, but no automatic host placement outside explicitly owner-authorized fabric.

## Invariants
Any implicit `parent_host or pick_host` becomes `explicit_pin if authorized else schedule(task)`; if a parent owns workspace on host A, child can launch host B with graph edge and correct task context. Use current native pane/workspace creation, never spawn a second agent where an existing session is still live or uncertain. Keep agent ID, task ID, mission ID, Git issue, parent lineage, room grants, audit and timelines stable; route terminal streams to actual remote host. No hardwired agent-family default to local.

## Exit gate
The launch-path matrix from Phase 0 has 100% scheduling enforcement coverage; regression tests on old CLI/manual workflows pass; denylist cannot be bypassed through helpers or special UI actions.
