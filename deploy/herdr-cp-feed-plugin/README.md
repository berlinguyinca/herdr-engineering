# herdr-cp-feed-plugin

A **native [herdr](https://herdr.dev) plugin** that emits workspace/agent
lifecycle events from the herdr host to the **HerdR control plane**
(`POST /api/v1/ingest` on `dev.lan`). This is the Option-A feed source: instead
of custom per-host reporters, we instrument the real herdr system itself.

Herdr runs a plugin for every lifecycle event it already produces, so this plugin
needs no polling, no daemon, and no per-host agent code — just link it and it
follows herdr's own event stream.

## How it maps herdr → control plane

| herdr entity            | control-plane entity | event_type               |
| ----------------------- | -------------------- | ------------------------ |
| this machine (hostname) | `host`               | `host.registered` (startup) / `host.heartbeat` (workspace.focused/created) |
| a **workspace**         | `mission`            | `mission.started` / `mission.updated` / `mission.ended` |
| a **pane / agent**      | `session`            | `session.started` / `session.updated` / `session.terminated` |

Every session payload carries `host_id` (the machine) and `mission_id` (the
workspace), so the control plane links hosts ↔ missions ↔ sessions.

The mapping is a pure function (`mapEvent` in `emit.js`) — unit-tested in
`emit.test.js` (`node --test emit.test.js`, 15 tests).

## Install / link on a host

1. Ensure Node ≥ 18 is installed (`node --version`).
2. Link the plugin directory (local authoring — no build step):

   ```sh
   herdr plugin link /path/to/herdr-engineering/deploy/herdr-cp-feed-plugin
   herdr plugin list
   ```

   The plugin is registered globally for the current user and available in every
   herdr session. It must be linked on **each** host you want tracked (bender,
   beast, macbook-pro, …).

3. Join the host to the control plane. The plugin reads
   `~/.config/herdr-engineering/host.json` (written by `herdr-eng fleet join`),
   then fetches its configuration (control-plane URL, ingest API key, ...) from
   `GET /api/v1/fleet/config` and caches it in
   `~/.cache/herdr-engineering/fleet-config.json` (mode 0600) so it keeps working
   while the control plane is unreachable. A revoked token (HTTP 401) deletes
   the cache. Redirects are never followed. `HERDR_CP_URL`, `HERDR_CP_API_KEY`
   and the `cp-url` config file are no longer read.

4. Restart the herdr server (or it picks the plugin up on the next session
   restore) and watch events flow:

   ```sh
   herdr plugin log list --plugin herdr.cp-feed
   curl -s http://dev.lan/api/v1/missions
   curl -s http://dev.lan/api/v1/hosts
   curl -s http://dev.lan/api/v1/sessions
   ```

> **Note on the web UI**: the control-plane web hydrates from Postgres **once at
> startup**, so after new events arrive you may need to
> `docker compose restart control-plane-web` to see them in the API. Live
> updates stream over SSE once connected. (See the control-plane README.)

## Best-effort & non-blocking

`emit.js` is fire-and-forget: a 2 s timeout, all errors swallowed (logged to
stderr), and envelopes sent in parallel. A control-plane outage never breaks or
slows the herdr host. `send()` never throws to herdr.

## Tests

```sh
cd deploy/herdr-cp-feed-plugin
npm test
```

## Current session snapshots

The event hooks enrich sessions with canonical Git repository identity and a
full commit SHA from the owning pane's local checkout. Credentials in origin
URLs are removed. Git commands never contact the remote forge. Metadata is
cached for 30 seconds per directory, with 128 entries, 1.5-second command
timeouts and 4 KiB output limits. Native session revisions remain separate.

Hooks that supply `HERDR_SOCKET_PATH` can resolve their named server through
an exact, unique match in `herdr session list --json`. Unknown server routes
remain telemetry-only.

For already-running sessions or hosts whose hooks omit the server route, run
the snapshot companion on each owning host:

```sh
node snapshot.js --once --dry-run
node snapshot.js --once
node snapshot.js --loop 30
```

The dry run prints counts and sends nothing. The companion discovers running
named servers from Herdr's registry, reads their structured snapshots, and
publishes sessions with native terminal identity. It does not start, stop or
steer sessions. Missing conversation identity or readiness is never invented.
The ingest API key comes from the central config (`ingest.api_key`) and is sent
only as the API key header. If the host config is unavailable the snapshot
companion prints `host config unavailable: ...` to stderr and exits 2.
Redirects are rejected. Each pass reports failures and truncation explicitly;
collection is limited to 8 servers, 128 sessions and a 20-second work budget.
The loop runs a new pass 30 seconds after the preceding pass finishes. It sends
changed sessions immediately and unchanged sessions at most once every five
minutes, retaining at most 256 sent records. Failed or missing observations do
not mark previously seen sessions finished. Unchanged terminal sessions do not
heartbeat, so finished/failed notices do not repeat while their record remains
in the bounded cache; a changed terminal record is still published.

Install all runtime files together: `herdr-plugin.toml`, `package.json`,
`emit.js`, `enrich.js`, `git-metadata.js`, `host-config.js`, `inventory.js`, `session-model.js`, `session-transcript.js`, `session-helpers.js`, `report-worker.js`, and `snapshot.js`. The snapshot
companion can run under systemd or launchd independently of native Herdr and
Pi processes; deploying it requires no native session restart.

The `herdr-cp-sessions.service` template expects the complete runtime bundle at
`~/.local/share/herdr-cp-feed-plugin-current` (a symlink to an immutable release
is suitable). Install the unit in `~/.config/systemd/user/` and configure any
host overrides in `~/.config/herdr-engineering/session-feed.env`, mode `0600`.
Supported environment values include `HOSTNAME` for the existing control-plane
host ID and `HERDR_BIN`; the control-plane URL and key are never set here. On macOS,
use launchd to run the same command with the installed Node executable; a
private wrapper can load the same environment file before execution. Activate
the companion only after the receiving backend supports these payload fields.

## Fleet rollout and native hooks

`deploy/deploy-fleet-agents.sh` ships the complete runtime bundle and installs
native integrations for installed Codex, Claude Code, and Pi CLIs on each host.
It uses the host's `herdr integration install TARGET` command, which merges
existing hooks/settings. Before each installation it stores existing managed
configuration and hook files in a private directory under
`~/.local/state/herdr-engineering/hook-backups/`. Installer output stays in that
directory, with mode 0600, rather than appearing in deployment logs. Failed
installations restore the managed files and make the rollout exit nonzero;
other installed integrations are still attempted. Missing runtime CLIs are
reported as skipped; an installed CLI with missing configuration is a failure.
Custom `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and `PI_CODING_AGENT_DIR` paths are
respected when provided in the remote execution environment.

Repeated installation is safe and preserves unrelated hooks and settings.
Only the companion feeds restart during rollout. Native agents are never
restarted or sent input. Hook installation applies on the runtime's next
start/resume; it cannot by itself identify a conversation already running
without its native session metadata. The rollout reports hook installation,
not proof that every existing session has emitted its transcript. Verify
recording separately through the live session API after deployment.

## Files

- `herdr-plugin.toml` — herdr manifest: startup hook + `[[events]]` subscriptions.
- `host-config.js` — central host-config client (host.json + cached fleet config).
- `emit.js` — the emitter (`mapEvent` mapping + `resolveUrl` + best-effort `send`).
- `emit.test.js` — unit tests for the mapping and URL resolution.
- `package.json` — marks the dir as an ES module + `npm test` wrapper.
