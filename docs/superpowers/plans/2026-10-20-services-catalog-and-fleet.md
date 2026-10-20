# Services catalog and fleet visibility

Goal: explain the fleet service registry, group it into a searchable catalog, retire dead entries reversibly, and extend authenticated Pi mission collection to existing hosts.

## Design

Keep durable service identities and history. Add read-time catalog metadata and lifecycle labels. Unavailable services archive after seven days without recovery; repeated unavailable reports do not reset the transition age. Healthy services never archive merely because old reporters omitted heartbeats. Missing/invalid timestamps stay unknown. Returning healthy registrations reappear. Existing list consumers retain all records; the web defaults to current entries with an explicit archived view.

Group by explicitly associated project where known, otherwise service type and host. Show original listener versus exposed route, owner/process, report age, status evidence and purpose. Never invent project ownership or expose command arguments/environment. Future fabric reports include bounded working-directory basename and periodic control-plane liveness.

Collectors run from immutable source on existing fleet hosts, authenticate local UID/PID and nonce-bound runtime identity, and do not restart Pi. Darwin requires a verified platform-specific peer identity path before enablement. Active older Pi processes cannot gain missing output instrumentation without reload; update installed code safely and report that limitation.

## Execution and verification

- Backend owner: catalog/lifecycle projection, replay of unavailable transition, bounds and API regression tests.
- Frontend owner: grouped search/filter catalog, contextual detail, persistent controls, browser tests and responsive checks.
- Provenance owner: safe process directory context, original port, bounded control-plane heartbeat, scanner tests.
- Fleet owner: authenticated Darwin support and real platform tests, collector deployment and Pi capability audit.
- Leader: integration, independent review, Python/JS/browser validation, green PR, image rollout with backups, host-by-host native agent convergence and production checks.

No durable event deletion, new dependencies, active Pi restart or mission resume. Rollback uses the previous web image and immutable native source.
