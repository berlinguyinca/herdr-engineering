# Focused mission browsing

Goal: reduce an observed 32,208px mission list and duplicate detail content while retaining live wait/stall visibility and full inspection.

## Behavior contract

- Show compact searchable mission rows: bounded two-line title, host, lifecycle and telemetry confidence, one activity/wait line, measured progress, worker count and one report-age clock.
- Prioritize current running/attention missions; separate last-known/disconnected evidence in a collapsed counted group. Never turn disconnected blocked snapshots into current incidents. Keep completed and recorded history discoverable without a second enormous grid.
- Preserve exact source namespaces and keys; merge live/recorded only on exact matching key, never bare mission ID guesses.
- Filter by search, host and status, with explicit totals and empty-state recovery. Preserve filter/focus/open state during refresh. Poll live every five seconds, clocks every second; recorded history every 30 seconds or on SSE. Stop hidden/navigation work and prevent overlapping requests.
- Detail immediately shows current activity, wait reason/duration, blockers and real progress. Full mission brief, IDs/revisions and diagnostics remain available in persistent disclosures. Active workers lead; ended attempts form a collapsed group. Remove duplicate worker listing and empty diagnostics.
- Preserve raw bounded evidence and existing tests for stale/disconnected/updating correctness, escaping, route races and zero mutation. Format weighted values for people without fabricating acceptance percentages.

## Ownership and validation

List/frontend owner: new catalog module, app wiring and list/browser tests. Detail owner: mission-live-view module and focused unit regressions. Leader: integration, independent review, full JavaScript/browser checks, affected Python checks, green PR and web-only rollout with historical/collector preservation. Verify actual fleet data on desktop/mobile and compare visible density, search/status routing, open panels across polls and honest telemetry labels.

No backend contract rewrite, new dependency, mission controls, Pi restart or telemetry notification changes.
