# ONE-SHOT IMPLEMENTATION PROMPT — Herdr Fabric Build Cache
You are the implementation lead working inside the existing Herdr Engineering codebase. The attached package `herdr-fabric-build-cache-20261104.zip` is a binding specification, but live code and operator configuration are the authoritative truth. This is a multi-stage task: execute autonomously only to the extent the environment and authorization safely permit.

## Mandatory first action
Locate the zip in the Mac Downloads folder, or use the exact operator-provided path. If running on a remote machine, copy the zip from the Mac to that explicitly selected host before extraction (SSH/rsync/scp only with verified host identity and path). Verify sha256 against the provided manifest. Unpack into a unique `docs/specs/herdr-fabric-build-cache-20261104/` directory in the REAL `herdr-engineering` repository. Never assume zip is already on the remote host. Never guess a host or path; if remote transfer cannot be verified, operate only on accessible local files, document the blocker, and continue safe audit tasks. Do not overwrite user content.

## Objectives
Build a Herdr-native, fabric-wide Rust compilation cache with local sccache, optional shared authenticated S3-compatible cache, bounded quotas, no active-build deletion, cache-aware scheduling and traceability, a single Herdr dashboard panel and export to grafana.metabolomics.us. Add OPTIONAL CPU Slurm whole-build execution and later optional persistent sccache-dist workers on Futurama-managed cluster; keep Slurm out of critical path. Protect physical SSD writes. Reuse prior Herdr build gate, cleanup, control plane, Pi extension and CI code; do not duplicate.

## Prior code surfaces to check
`herdr_engineering/build_coord.py`, `build_cleanup.py`, `build_maintenance.py`, `control_plane/slurm_placement.py`, `config.py`, `fleet.py`, `cli.py`, `deploy/pi-build-gate/build-gate.ts`, existing API/SSE/dashboard, Ansible and tests. Read all current code; names may have changed. Especially reconcile contradictory build gate slot defaults in documentation vs implementation.

## Strict operational constraints
- Use only explicitly identified host and repository. Verify branch, Git SHA, dirty files and available toolchains first; preserve all user changes.
- No production changes, remote deployments, auth policy changes, cache deletion or Slurm job submissions without explicit authorization and acceptance tests. Use simulated test backends first.
- Reuse current APIs, permissions, deployment tools, Grafana target, UI patterns, metrics and storage service. No local Grafana, no parallel Herdr backend, no custom cache protocol, no shared writable Cargo target directory.
- Independent Futurama Slurm cluster is separate from other Slurm/CI clusters. Futurama control plane should not absorb compilation or bulk cache I/O.
- Cache misses or storage outages do not break local builds. Slurm must be opt-in and recoverable; GPU nodes not allocated for CPU builds by default.
- No secrets embedded in specs, logs or agent prompts. Restrict cache scopes by trust domain, compiler fingerprint and permissions.
- Work with small-model context: one micro-spec at a time, short handoffs, never broaden scope to adjacent projects. No unsupported assertions about success.

## Execute
1. Read README, ARCHITECTURE, `ops/audit-checklist.md`, all contracts and acceptance matrix.
2. Execute `specs/00-audit.md`; produce `AUDIT.md` and a detailed reuse/gap map. Confirm any already completed micro-specs with tests, mark reused work, and skip duplication.
3. Execute micro-specs 01–14 strictly in order. Each stage: tiny file-scoped plan; failing tests; implementation; targeted + regression tests; handoff; commit only files belonging to that stage. Review every stage before advancing. Commit messages `feat(build-cache): <scope>` or `test(build-cache): <scope>`.
4. Collect baseline before optimizations. Do not report hypothetical writes saved as observed facts.
5. Run all tests in `tests/acceptance-matrix.md`, including race, corruption, outage, controller restart, cross-architecture, Slurm offline and opt-in authorization.
6. Keep feature flags OFF by default. If live backend or deployment approval is unavailable, finish implementation/testable adapters and document the exact unmet deployment gates rather than improvising infrastructure.
7. Deliver `IMPLEMENTATION_REPORT.md` including code diff/commits, reused existing components, tests/metrics, confirmed cache backend, readiness by host, rollback procedure, skipped/blocked work and explicit approvals needed. Include copy-paste pilot enable/disable commands verified against actual current CLI.

## Definition of done
A local Bender pilot can compile correctly with optional cache and collect actual NVMe write measurements; a compatible second host can safely share cached compiler results against a verified backend; existing Herdr build gating and cleanup remain safe; Herdr dashboard reports observed metrics; optional Slurm integration is independently guarded and proven on a test partition or clearly marked blocked; disabling feature flags returns to existing build behavior. No invented completion claims.
