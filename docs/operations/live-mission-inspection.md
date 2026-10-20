# Live Pi mission inspection

HerdR's live mission section is a read-only projection of Pi's owned mission state. It is separate from recorded mission lifecycle events and attention notifications. Collection and browser refreshes do not change recorded missions, append ordinary events, or produce global SSE notifications.

The native host collector queries private local Pi control sockets and requires matching UID, PID, instance, session, and nonce responses. It accepts only runtime-owned mission IDs and snapshot revisions matching the live reply. A missing or changing snapshot appears as updating; old details remain explicitly last known. A missing collector appears stale or disconnected, never as a fabricated mission crash.

`GET /api/v1/mission-activity` returns bounded summaries. `GET /api/v1/missions/pi%3AMSN-ID/activity` returns a worker inspection projection. The mission list places live Pi entries above recorded history and keeps the `pi:` namespace distinct from legacy bare mission IDs. Visible pages poll every five seconds and age labels update locally every second. Navigation and hidden pages stop polling.

Worker inspection includes captured activity, provider/model routes, waits, output and progress ages, bounded redacted tool output, test counts, changed files, and recovery evidence. It excludes private reasoning and arbitrary tool arguments. Collection timestamps and activity timestamps remain separate; a collector heartbeat cannot make an old snapshot current.

## Deployment order

1. Back up the running server's current entity snapshots and raw event streams before replacing an older server. Verify PostgreSQL connectivity and replay parity; a configured `source=postgres` health field alone does not prove connectivity.
2. Build a uniquely tagged image from the published commit and replace only `control-plane-web` with the same environment, network, and persistent services. Preserve PostgreSQL/RustFS volumes and active Pi processes.
3. Verify `/api/v1/health` advertises `mission_activity_v1`, and the bulk/detail GET routes work. The collector refuses to publish to older servers, whose generic ingest fallback would otherwise turn unknown telemetry into ordinary events.
4. Place the published collector checkout at `~/.local/share/herdr-engineering/mission-feed/releases/<full-commit>` and atomically point `current` to it. The service uses that source with the existing Python environment, so unrelated native services need no package replacement or restart.
5. Install `deploy/mission-feed/herdr-eng-mission-feed.service` into `~/.config/systemd/user/`, reload the user manager, and enable/start that unit. Its default server is `http://dev.lan`; an optional private `~/.config/herdr-engineering/mission-feed.env` can override `HERDR_MISSION_FEED_URL` and provide `HERDR_CP_API_KEY`.

Run a single collection pass with:

```bash
herdr-eng control-plane mission-feed --control-plane http://dev.lan --host-id "$(hostname)" --once
```

The JSON result reports accepted projections and fixed rejection/error categories, never arbitrary server response bodies or credentials. Only one daemon instance runs per user. SIGTERM stops collection gracefully. Linux peer credential validation is supported; validate macOS peer-credential availability before enabling the service there.

## Recovery and verification

The October 2026 rollout encountered an existing stopped database. Recovery preserved original event IDs, timestamps, payloads, and stream sequences and backfilled missing IDs idempotently. Historical duplicate stream sequences are retained; replay, recent-event lookup, and retention must distinguish occurrences instead of treating sequences as array offsets. New events allocate above the maximum surviving sequence.

Before cutover, compare replayed entity IDs and meaningful status, title, process, model, and association fields with the live API. Export and backfill the final live delta before replacing the old web process. Keep backups and rollback image provenance private under `~/.local/state/herdr-engineering/rollouts/`.

After cutover, verify real Pi projections, namespaced mission navigation, worker detail, local age updates, updating/stale/disconnected states, and absence of telemetry events in the ordinary activity feed. Restarting the collector must not restart or resume Pi missions. Rollback stops the collector first, then restores the previous web image and assets; it does not delete database rows or volumes.
