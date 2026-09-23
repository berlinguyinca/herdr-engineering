# Shared Filesystem / Contributor Workspace

This is a core capability.

Do NOT invent a HerdR distributed filesystem. Select a mature existing filesystem/synchronization layer below HerdR, then let Powerpack configure/discover/health-check/present it.

During Phase 0 compare appropriate current technologies for Linux + macOS + private/Tailscale networking. Evaluate:
- consistency/concurrency model
- offline/reconnect behavior
- locking/conflicts
- performance and large files
- permissions/identity
- failure recovery
- operational burden
- security
- backup/snapshot suitability

Do not hard-code a backend before this evaluation.

Logical namespace:
shared/
- users/
- projects/
- artifacts/
- handoffs/
- sessions/
- datasets/
- screenshots/
- reports/
- scratch/

Requirements:
- available to enabled contributors/agents on fry, beast, bender, macbook-m4 and future nodes
- stable logical shared-root pointer/environment variable
- Linux/macOS support
- private networking
- explicit permissions
- safe concurrency
- documented offline/recovery behavior
- atomic metadata writes where needed
- no credential/home-directory synchronization by default
- quotas/cleanup for scratch/large artifacts
- capacity/sync health in Powerpack doctor
- shared failure cannot damage local worktrees/projects

Prefer a verified existing HerdR file-viewer plugin to browse/open this shared root.

Artifacts such as screenshots, reports, generated HTML, review output and handoffs should use stable shared references when appropriate.
