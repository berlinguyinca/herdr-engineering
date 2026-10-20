# Topology and current work

**Goal:** Make the native workspace show current work first, with stopped, terminal and unconfirmed observations hidden until explicitly requested. Retain all history and identities.

**Design:** Replace the unbounded topology tree with a compact host/project explorer. Default to current observations and folded host groups, with search, type/host/status filters, explicit unassigned observations and bounded drill-downs. Join only explicit host, session, mission and canonical repository/project identities. Directory labels are provenance, not project identity. Prefer links to existing inspectors over duplicate dashboards. CI gets a compact rollup and typed outcomes rather than a repository dump.

**Visibility:** Shared freshness helpers keep lifecycle separate from display. Current requires a known open lifecycle and a recent report, allowing a small clock skew. Use the existing service policy of fifteen minutes; runtime missions additionally retain their stricter existing telemetry quality rules. Fresh planned/recorded missions remain visible as records without claiming execution. Stale/missing telemetry becomes Unconfirmed, never automatically completed or dead. Failed/Finished/Archived history remains available through explicit filters. No database deletion.

**Native behavior:** Reuse existing tokens, compact rows, outcome tabs and validated endpoint links. Keep search, focus, expansion and selected context stable during independent refreshes. Missing API data retains last-known observations with an error, not a false healthy zero. Orphan host records remain accessible without inventing relationships.

- [x] Implement visibility selectors and regression tests for fresh/stale/native working, terminal retention, timing-only expiry and explicit history access.
- [x] Implement topology model/view, stable polling and route cleanup; cover unknown hosts, unassigned projects, bounded rows and unsafe URLs.
- [ ] Run targeted/full browser and unit checks, review correctness and desktop/mobile presentation, then merge with green CI.
- [ ] Deploy web-only using the current runtime image, preserve PostgreSQL history and native processes, and verify the live filtered counts and inspectors.

## Service ownership and safe discovery

Primary Services, Overview and Topology include only services with verified HerdR launch provenance. System and unassigned observations remain on a separate System services page. A session ID or directory alone is not launch proof. Keep recorded provenance when the owning pane ends; require a fresh proof for a new process instance.

Existing fabric forwarders also carry active streams. Do not restart them or upgrade their shared environment for metadata collection. A separate read-only observer inspects actual listeners and native process ancestry without writing StateStore, leases, routing or forwarding. Metadata updates to legacy services cannot refresh their health. Newly discovered verified HerdR listeners use stable host/process-birth/port identities, all dynamic ports, local-only endpoint scope and no invented public URL. Exclude browser debugging endpoints. Report absence only after a complete scan. Metadata older than its 90-second validity becomes Unconfirmed.

CI plots describe the loaded run sample, UTC outcomes and measured durations, with accessible table alternatives. Do not imply complete historical success rates from a recent window.
