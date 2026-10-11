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
node --test emit.test.js
```

## Files

- `herdr-plugin.toml` — herdr manifest: startup hook + `[[events]]` subscriptions.
- `emit.js` — the emitter (`mapEvent` mapping + `resolveUrl` + best-effort `send`).
- `emit.test.js` — unit tests for the mapping and URL resolution.
- `package.json` — marks the dir as an ES module + `npm test` wrapper.
