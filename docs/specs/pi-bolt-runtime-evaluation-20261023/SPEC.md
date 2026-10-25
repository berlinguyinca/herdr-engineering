# Pi-Bolt Runtime Evaluation and Herdr Integration

Status: Proposed; audit-first, no production default change.
Date: 2026-10-23
Scope: Pi Engineering / Herdr, Autospec, InferWeave integration.

## Objective
Evaluate Pi-Bolt as an interchangeable execution runtime for Pi Engineering agents. Improve startup latency, tool execution overhead, CPU and memory efficiency under concurrency without sacrificing correctness, security, observability, or upstream Pi compatibility. Vendor benchmark claims are hypotheses, not acceptance evidence.

## Non-goals
- Replacing Herdr orchestration, Autospec, InferWeave routing, or the collaboration layer.
- Rewriting Pi extensions to chase benchmarks without measurements.
- Changing model selection, billing, or gateway scheduling semantics.
- Silently making Pi-Bolt the default.

## Phase 0 — Repository and compatibility audit (mandatory)
Inspect the checked-out repositories, their actual versions, launchers, extension APIs, hooks, events, sessions, RPC protocols, worker spawning, deployment files, tests, and documentation. Locate Herdr, Pi Engineering, Autospec, InferWeave integration and collaboration modules; do not assume repository names or implementation details. Inspect upstream Pi and Pi-Bolt source, version history, release notes, license and dependency supply chain. Confirm the exact version requirements for Herdr integration, session restoration and extension support. Produce a gap matrix: feature, current implementation, Pi-Bolt mechanism, risk, test, decision. Reuse/refactor existing adapters, do not duplicate orchestration or event stores.

## Architecture
Keep Herdr as control plane. Introduce or extend one runtime adapter boundary (only if existing boundary is insufficient): `standard-pi`, `pi-bolt`, optionally `pi-bolt-jit`. The adapter must preserve lifecycle (spawn/start/ready/tool-call/stream/pause/resume/cancel/exit), session identity and restore, worktree/branch/issue associations, model/provider selection, environment isolation, tool permissions, extension hooks, trace IDs, resource metering, and error propagation. Runtime choice should be configurable at global, workspace, mission, worker-role levels with documented precedence. Support per-agent canary and deterministic fallback before task start; do not blindly replay non-idempotent tool actions on runtime failure.

## Integration requirements
1. Herdr workbench: show runtime type/build/version per agent, spawn lineage, phase durations, CPU/RSS, tool latencies, failure/fallback reasons; retain existing mission graph and git issue links.
2. Collaboration: preserve room membership, external agent identities, chat/annotation permissions, message provenance, cross-system authentication, event ordering and reconnect semantics. Runtime is not a new collaboration service.
3. Autospec and Planotator: plans may receive optional approval before implementation; autonomous bypass must respect existing policy. Both Autospec and standalone missions work. No runtime may bypass quality gates.
4. Pi extensions: enumerate and test every installed extension/skill, including OpenViking, memory, observability, subagent management, tool interception, model selection, and custom hooks. Compare interpreted/JIT/bundled behavior; log incompatibilities.
5. InferWeave: preserve provider URL/auth, model identity, quotas, owner priority, request accounting, session affinity, context policies, token streaming and billing. Runtime metrics must be distinct from inference metrics.
6. Remote nodes: Linux/macOS architecture compatibility, Docker/Apptainer, remote execution, Tailscale transport, graceful drain, deployment rollback, and heterogeneous worker capabilities.
7. Security: no token leaks to logs or child processes; least privilege; signed/checksummed binaries where available; pin versions and dependencies; review network/filesystem permissions; no untrusted auto-update.

## Benchmark protocol
Test standard Pi, Pi-Bolt standard and Pi-Bolt JIT where supported, on the same hosts and tasks, same models, context, prompt, extensions, cache state and network conditions. Run warm and cold trials; at least 10 repetitions per scenario; report median/p95 and uncertainty. Record model prefill/decode and gateway queue time separately so model/network variance does not masquerade as runtime speedup. Include:
- No-op startup and ready-to-first-token.
- File-heavy read/edit/write, shell-heavy, and tool-heavy tasks.
- 1, 5, 12, and 24 concurrent agents, with resource saturation indicators.
- Full Herdr mission with plans, optional review, child agents, issue links, failed implementation branch and resumed session.
- Collaboration room with remote agents and reconnect.
- Long-running soak, cancel/restart, crash recovery, extension failures.
Capture wall time, CPU seconds, RSS/peak RSS, file/tool latency, tokens, quality/test success, event loss/duplication, trace continuity, rollback outcomes, and infrastructure utilization. No benchmark may disable required extensions or gates in only one arm.

## Acceptance criteria
- 100% pass of critical lifecycle, session, permissions, plan gates, collaboration, issue linkage, provider routing, accounting and trace tests.
- No unreviewed security regressions, missing audit events, or corrupted sessions.
- Statistically credible improvement in at least one important bottleneck (startup, host resources, or total completion time) with no material quality regression.
- Rollback to standard Pi demonstrated without data loss or duplicate side effects.
- All incompatibilities documented; unsupported hosts stay on standard Pi.
- Production rollout requires human approval after report; otherwise retain standard Pi.

## Work breakdown / issues
PB-001 Inventory repositories, runtime integration points and versions (dependency for all).
PB-002 Review Pi-Bolt source, license, release, security and compatibility matrix (after 001).
PB-003 Define runtime adapter contract and configuration precedence (after 001-002).
PB-004 Implement adapter and feature detection using existing launcher paths (after 003).
PB-005 Implement safe fallback, cancellation, session restore and rollback (after 004).
PB-006 Validate extension modes, skill loading and security controls (after 004).
PB-007 Validate Autospec, Planotator and standalone execution gates (after 004).
PB-008 Validate Herdr lineage, git issue graph and collaboration events (after 004).
PB-009 Validate InferWeave provider/routing/accounting integration (after 004).
PB-010 Build reproducible benchmark harness and baseline (after 001-003).
PB-011 Run cold/warm/concurrency/soak benchmarks (after 005-010).
PB-012 Add runtime metrics and dashboard views using existing telemetry stack (after 004, 010).
PB-013 Produce comparison report, recommendation and canary plan (after 006-012).
PB-014 Controlled canary, explicit go/no-go and rollback drill (after 013 and approval).

## Required deliverables
- `docs/specs/.../audit.md`: verified code locations and reuse decisions.
- `compatibility-matrix.md`: runtime/version/extension/host results.
- `benchmark-plan.md`, machine-readable raw measurements, reproducible scripts, comparison plots.
- Runtime adapter implementation/tests in the existing appropriate repo(s).
- Existing Herdr dashboard integration with runtime metrics and traceable mission graph.
- `rollout.md`: configuration, canary, rollback, security, version pinning.
- `decision.md`: measured gains, limitations, recommendation and explicit approval gate.

## Execution rules
Do not invent codebase features. Audit before coding. Prefer refactoring existing components; never create parallel launchers, schedulers, graphs or telemetry pipelines. Keep each change small and testable, link issues and commits to existing Herdr mission graph. Separate implementation and review roles when available. Maintain passing tests, preserve compatibility and stop for approval before production default changes.
