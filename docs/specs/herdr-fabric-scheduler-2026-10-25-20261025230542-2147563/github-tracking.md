# Herdr Fabric Resource Scheduler — P00–P10 acceptance

Specification: `docs/specs/herdr-fabric-scheduler-2026-10-25-20261025230542-2147563/`.
Evidence: `release-safety-audit.md` in that directory. Implementation remains in
progress; narrow passing checks do not constitute full phase acceptance.

- [x] Retrieve specification from the Mac into a unique directory; preserve archive hash.
- [x] Implement shared admission, durable reservation/lease projections, and atomic submission claims.
- [x] Verify 100-request PostgreSQL contention, crash rollback, and cross-coordinator policy changes.
- [x] Implement host-authenticated resource reporting and explicit Mac manual-only defaults.
- [x] Verify live legacy Mac opt-outs for mac.lan and macbook-pro.
- [x] Implement native family callbacks and verified Git/worktree preparation with registry artifacts.
- [x] Implement Linux sandbox/cgroup stop verification and demonstrate process-tree write revocation.
- [x] Implement queue retry/draining, placement history, policy editing, and read-only historical simulation.
- [ ] Finish every audited CLI/dashboard/Pi/Claude/Codex/AutoSpec/Plannator/handoff/subagent launch integration.
- [ ] Persist and report complete process lifecycle/accounting across all native families.
- [ ] Complete runtime-specific private checkpoint/resume/fencing certification; keep unsupported migration disabled.
- [ ] Complete full fault-injection matrix and measured load/baseline comparison.
- [ ] Complete multi-host Linux/macOS canary, flag rollout, and verified rollback.
- [ ] Complete repository-wide tests, scoped review, cohesive commits/PRs, and final requirement-by-requirement acceptance.

InferWeave retains inference/model/GPU-worker scheduling ownership throughout.
Concurrent artifact/publication/handoff work must be preserved during integration.
