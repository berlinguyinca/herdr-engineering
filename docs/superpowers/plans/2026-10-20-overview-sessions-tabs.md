# Current-work overview and lifecycle tabs

Goal: make Overview and Sessions useful for current work, and place failed/closed/finished mission/session records in explicit counted tabs.

## Contract

Current, Failed and Finished tabs share lifecycle classification. Terminal status wins over stale activity; terminal stage refines generic active status. Missing or stale telemetry never means finished. Recorded nonterminal missions remain current/unconfirmed rather than being called finished. Exact source namespaces and record IDs remain independent.

Overview summarizes current missions, sessions, services and hosts with actionable links, bounded meaningful activity and honest report freshness. Independent endpoint loading/failure must not blank healthy panels or fabricate zero counts. Counts link to the correct outcome/filter tabs.

Sessions use compact searchable paginated rows, explicit host/project context and last-reported status. Details retain inspection, full metadata and event history in disclosures; terminal sessions do not show steering forms. Encode IDs once and cancel in-flight detail reads on navigation.

Preserve search, tabs, pagination, focus and inspection across polling/SSE. Poll five seconds with bounded requests, local clocks, hidden/navigation cleanup and single-flight work. No ordinary telemetry notifications, new dependencies or backend lifecycle mutations.

## Validation and rollout

Test lifecycle aliases/authority, partial/empty sources, terminal records under stale/fresh reports, keyboard tabs, deep-link filter application, namespace-safe session links and cancellation. Run full client/component/browser checks, independent review and green PR; replace web only with the same environment/ports, verify current data/history and desktop/mobile production behavior, and monitor post-merge CI.
