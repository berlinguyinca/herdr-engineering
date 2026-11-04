# Phase 5 — distributed environment/worktree readiness
Before launch, preflight destination for repo access, cloned workspace, exact commit, branch/worktree, tool version, policy-compliant credentials and disk. Define workspace manifest `{workspace_id,repo_url,revision,branch,worktree_id,task_id,artifact_refs,environment_digest,owner,acl}` stored in existing control plane. Snapshot worktree state and dirty changes intentionally; never silently discard or replay uncommitted changes. Use separate local worktrees on each host with explicit ownership, lock/merge strategy and safe handoff artifacts; never a single mutable NFS worktree shared by concurrently editing agents.

## Data transport
Prefer Git for committed code, existing secure repo sync for explicitly allowed dirty changes, existing RustFS artifact service for build outputs, snapshots and large binary files. Artifacts must be scoped and authorized, checksum-verified, versioned and encrypted in transit; respect private/public boundary, do NOT publish session/credential/checkpoint state through public artifact URLs. Cache locality improves score but cannot supersede security/correctness. Reuse existing RustFS integration if present; don't build new S3 gateway for scheduler.

## Migration preparation
Persist resumable agent session metadata/continuation pointers independently from terminal process IDs. Check version-compatible harness resume ability: Claude/Pi/Codex session semantics differ; define family adapters and explicit `cannot_checkpoint` state. Generate relocation plan with branch/commit, dirty tree status, in-flight command safety and tool idempotency. For unsafe work, drain and wait, not terminate.

## Acceptance
Cross-host parent→child with valid worktree, artifact access controls, remote logs, successful build, no dirty file loss, no cross-user credential leak; simulate 2 workers trying same branch and show single-writer gate or isolated branch strategy.
