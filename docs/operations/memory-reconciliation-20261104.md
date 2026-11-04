# Memory implementation reconciliation — November 4, 2026

The October 28 session resumed after the unified Hindsight work had landed in
both repositories. Continue from the current implementation rather than merging
the older, unfinished gateway and dashboard.

## Architecture to reuse

| Concern | Current implementation | October work disposition |
| --- | --- | --- |
| Memory storage and semantic retrieval | Existing Hindsight deployment and canonical `herdr-memory` CLI | Keep; no second engine or canonical PostgreSQL journal |
| Harness installation | `memory_install.py`, managed overlays and the existing lock | Extend; do not install the older standalone installer over these overlays |
| MCP and lifecycle | `memory_mcp.py`; Pi's existing durable-memory provider | Extend verified surfaces; retain capability-specific fallbacks |
| Dashboard | `memory_explorer.py`, `memory-view.js`, authenticated `/api/v1/memory` reads | Keep; do not merge the second Memory view or native provider proxy |
| Deployment and backups | Existing optional memory Compose profile and deployment scripts | Keep; do not add the old access-network overlay |
| Cross-project routing | Canonical client's path/default-project resolver | Carry forward registered-worktree verification only |

The old worktrees and their uncommitted changes remain preserved. In particular,
the old `0015_assistant_memory_sync.sql` is not a migration to apply: current
migration 0015 has another purpose. The former security PR #317 is already merged.

## Fresh evidence

On the resumed host, installed client 0.1.5 reports the canary project healthy,
authenticated, and not degraded, with zero pending, orphaned or dead-letter
writes. The existing Hindsight containers are healthy; the API is published on
loopback port 8897 and the database has no published host port. These observations
do not substitute for installed-session acceptance tests.

The remaining routing defect was reproduced with real temporary Git repositories:
an unregistered `.git` pointer, including a copied linked-worktree pointer,
inherited another project's mapping. The fix verifies the root against Git's
worktree registry while preserving the configured `default_project`. Five real
Git regression tests pass. The full client suite runs 105 tests successfully,
with four explicitly selected integration tests skipped.

MCP separately treated `"explicit": "false"` as explicit save intent because of
Python truthiness. The wrapper now rejects supplied non-Boolean values before
running the CLI. Boolean false requires evidence; Boolean true is forwarded as
caller-reported intent, not independent human verification.

The controller's own stale client distribution also overrode CLI-owned package
metadata in doctor. Package version and revision now come only from the install
owning the resolved CLI; missing provenance remains unknown. All 49 installer/MCP
tests pass, including a deliberately stale wrapper distribution.

## Remaining boundaries

Real Claude and Pi sessions retrieved synthetic private validation tokens using
the installed MCP surface and documented CLI fallback respectively. Their
synthetic memories were deleted afterward. A real Codex session exposed a
different issue: the unconfigured MCP tool approval policy refused recall when
approvals were unavailable. Adapter 3 adds a tool-specific recall approval and
keeps retention approval-gated. A subsequent actual Codex session with the
candidate policy completed the MCP call, retrieved the unknown reply token,
and its synthetic memory was deleted afterward. Merely listing an MCP tool did
not establish that it could execute.

The older automatic summary adapters are not installed by this reconciliation.
The current installer explicitly advertises automatic retention as unsupported.
If completion summaries are added, integrate them into the existing installer
and Pi provider, preserve summary/fact separation and session privacy guards, and
verify actual harness completion events before enabling them. Do not revive the
old duplicate gateway to support this feature.

OpenViking migration is not established by these tests. Preserve source data and
configuration until an authorized export and provenance/count reconciliation are
available. No public exposure, production Tailscale changes or legacy deletion
is needed for the fixes above.
