# System architecture and module responsibilities

## Logical boundaries
1. Artifact registry (existing control-plane service): canonical IDs, ownership, ACL, provenance, versions, collection membership, query/search, audit. PostgreSQL authoritative.
2. Artifact bytes (existing `RustFSClient` S3-compatible): immutable blobs addressed by SHA-256, upload verification, retention and reference-safe GC; origin bucket permanently private.
3. Generation engine: a **job API** using existing worker and fabric scheduling capabilities. Profiles web, document, diagram, data/chart, presentation, report. Job workers run sandboxed toolchains; preserve source, assets, dependency lockfiles, reproducibility info and outputs.
4. Publication controller: desired state, human approval, provider reconciliation, audit, stable public logical routes, per-publication scoped policy.
5. Public data plane: isolated edge gateway; request authentication, immutable artifact delivery or explicit service proxy. Do not expose privileged control plane.
6. Exposure adapters: Tailscale Funnel initially, pluggable Cloudflare Tunnel, dedicated VPS reverse-proxy/WireGuard, direct ingress, K8s Gateway API. Private connectivity provider is independent from exposure provider.
7. Dashboard: read-only previews/edit history/review, permissions/sharing, status. Author/create/modify through CLI and agent slash commands; ordinary manual attachments acceptable. No full web editor.

## Workflow boundaries
CLI/agent → authenticated control-plane API → persistent metadata, jobs and blob storage → events → dashboard and collaborators. Public viewers → edge → publication enforcement → isolated public artifact snapshot or narrow registered service proxy. No path from public edge to general control-plane admin routes.

## Contracts
An artifact logical ID is stable; versions reference immutable blob and source snapshots. An artifact can have many relationships to session/mission/room/issue/commit. Authorization always keyed to artifact/resource principal, not shared hash. Publishing internally does not make publicly accessible. Creating a public URL is a separate approval-controlled action.

## Compatibility
No breaking changes to existing callers without versioned API/deprecation. Central fleet config remains source for host endpoints. Extend collaboration attachment transport; do not fork it. Handle worker failures and private-only installations.
