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

3. Point the plugin at the control plane. Resolution order:
   1. a file named `cp-url` in the plugin config dir
      (`herdr plugin config-dir herdr.cp-feed` prints it), or
   2. the `HERDR_CP_URL` environment variable on the herdr server process, or
   3. default `http://dev.lan` (works when split-DNS is configured fleet-wide).

   Example using the config file:

   ```sh
   CFG=$(herdr plugin config-dir herdr.cp-feed)
   echo -n "http://dev.lan" > "$CFG/cp-url"
   ```

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
HERDR_CP_URL=http://dev.lan node snapshot.js --once
HERDR_CP_URL=http://dev.lan node snapshot.js --loop 30
```

The dry run prints counts and sends nothing. The companion discovers running
named servers from Herdr's registry, reads their structured snapshots, and
publishes sessions with native terminal identity. It does not start, stop or
steer sessions. Missing conversation identity or readiness is never invented.
An optional `HERDR_CP_API_KEY` is sent only as the API key header; supply it
through the service's private environment configuration, not command arguments.
Redirects are rejected. Each pass reports failures and truncation explicitly;
collection is limited to 8 servers, 128 sessions and a 20-second work budget.
The loop runs a new pass 30 seconds after the preceding pass finishes. It sends
changed sessions immediately and unchanged sessions at most once every five
minutes, retaining at most 256 sent records. Failed or missing observations do
not mark previously seen sessions finished.

Install all runtime files together: `herdr-plugin.toml`, `package.json`,
`emit.js`, `enrich.js`, `git-metadata.js`, and `snapshot.js`. The snapshot
companion can run under systemd or launchd independently of native Herdr and
Pi processes; deploying it requires no native session restart.

The `herdr-cp-sessions.service` template expects the complete runtime bundle at
`~/.local/share/herdr-cp-feed-plugin-current` (a symlink to an immutable release
is suitable). Install the unit in `~/.config/systemd/user/` and configure any
host overrides in `~/.config/herdr-engineering/session-feed.env`, mode `0600`.
Supported environment values include `HOSTNAME` for the existing control-plane
host ID, `HERDR_CP_URL`, `HERDR_BIN`, and optional `HERDR_CP_API_KEY`. On macOS,
use launchd to run the same command with the installed Node executable; a
private wrapper can load the same environment file before execution. Activate
the companion only after the receiving backend supports these payload fields.

## Files

- `herdr-plugin.toml` — herdr manifest: startup hook + `[[events]]` subscriptions.
- `emit.js` — the emitter (`mapEvent` mapping + `resolveUrl` + best-effort `send`).
- `emit.test.js` — unit tests for the mapping and URL resolution.
- `package.json` — marks the dir as an ES module + `npm test` wrapper.
