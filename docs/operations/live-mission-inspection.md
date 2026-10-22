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
5. Install `deploy/mission-feed/herdr-eng-mission-feed.service` into `~/.config/systemd/user/`, reload the user manager, and enable/start that unit. The control-plane URL, host id and (only when keys are enabled) `ingest.api_key` come from the central fleet config through `~/.config/herdr-engineering/host.json` (`herdr-eng fleet join`); no env file is used.

Run a single collection pass with:

```bash
herdr-eng control-plane mission-feed --once      # URL and host id from host.json
```

The JSON result reports accepted projections and fixed rejection/error categories, never arbitrary server response bodies or credentials. Only one daemon instance runs per user. SIGTERM stops collection gracefully. Linux validates `SO_PEERCRED`. macOS validates the real socket UID through libc `getpeereid` and the PID through `LOCAL_PEERPID`, including Python builds without a `socket.getpeereid` wrapper. Missing or failed credential checks reject the source.

## Recovery and verification

The October 2026 rollout encountered an existing stopped database. Recovery preserved original event IDs, timestamps, payloads, and stream sequences and backfilled missing IDs idempotently. Historical duplicate stream sequences are retained; replay, recent-event lookup, and retention must distinguish occurrences instead of treating sequences as array offsets. New events allocate above the maximum surviving sequence.

Before cutover, compare replayed entity IDs and meaningful status, title, process, model, and association fields with the live API. Export and backfill the final live delta before replacing the old web process. Keep backups and rollback image provenance private under `~/.local/state/herdr-engineering/rollouts/`.

After cutover, verify real Pi projections, namespaced mission navigation, worker detail, local age updates, updating/stale/disconnected states, and absence of telemetry events in the ordinary activity feed. Restarting the collector must not restart or resume Pi missions. Rollback stops the collector first, then restores the previous web image and assets; it does not delete database rows or volumes.


## Additional hosts and macOS

Use the same immutable published collector revision on each host. The control directory
must match the directory actually used by that host's Pi sessions. On Linux, a user
systemd unit may inherit `XDG_RUNTIME_DIR` while terminal Pi sessions use `/tmp`.
Set `PI_ENGINEERING_CONTROL_DIR` in a unit drop-in to the proven native directory;
never merge arbitrary project snapshots or weaken directory/peer checks.

For macOS, render `deploy/mission-feed/com.herdr.mission-feed.plist` with absolute
`__HOME__`, the inventory `__HOST_ID__`, and `__CONTROL_DIR__` values using
`plistlib.load` / `plistlib.dump` (so paths are XML escaped). Install it as
`~/Library/LaunchAgents/com.herdr.mission-feed.plist`, mode 0600. Create the
`~/.local/state/herdr-engineering` log directory, then use
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.herdr.mission-feed.plist`.
The control plane, host id and (when keys are enabled) the ingest key come from
`~/.config/herdr-engineering/host.json` and the central fleet config it points at;
put no URL or credential in the plist.

macOS's long default temporary directory can exceed Pi's Unix socket path limit.
Use a short same-user directory such as `/tmp/pi-engineering-501`, created with
mode 0700, and export that exact `PI_ENGINEERING_CONTROL_DIR` in the Pi launcher's
or user's shell environment as well as the collector plist. Do not change global
`TMPDIR`. Existing Pi sessions keep their original environment until the operator
normally reloads or starts a new session; installing the collector does not restart
them. `launchctl print gui/$(id -u)/com.herdr.mission-feed` verifies the collector.

Stage newer Pi output capture with the repository's immutable
`ansible/files/stage-release.py --kind pi --revision <full-published-commit>` workflow.
It preserves unrelated settings and running processes. Rich output becomes available
when a session loads that release and produces tool output; old sessions and old
snapshots must continue to show unavailable/last-known evidence honestly.
