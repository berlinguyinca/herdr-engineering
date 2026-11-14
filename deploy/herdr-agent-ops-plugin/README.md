# herdr-agent-ops-plugin

Terminal Agent Operations map for Herdr. It wraps `herdr-eng agent-ops` (`herdr_engineering/agent_ops_tui.py`):
a colored Unicode character-cell map of the control plane's `/api/v1/agent-ops/snapshot`, kept live by
`/stream` (cursor resume, reconnect with backoff, `LIVE | DEGRADED | OFFLINE` status line). Read-only; the API is
metadata-only.

## Install (not done automatically)

1. `herdr-eng` must be on `PATH` (`herdr-eng --version`) and the control plane must run with
   `control_plane.agent_ops.enabled: true`.
2. Point it at the control plane: `HERDR_CP_URL` (default `http://127.0.0.1:8080`), `HERDR_CP_API_KEY` (viewer key),
   optionally `HERDR_AGENT_OPS_TOKEN` (or `HERDR_COLLAB_TOKEN`; a collab bearer token, without it you only see org-scoped, room-less events). Prefer the environment variable or `--token-file <path>` over `--token <value>`: an argv token is visible to every local user in the process list (`ps`), and the pane prints a warning when you use it.
3. Link for local use: `herdr plugin link /path/to/herdr-engineering/deploy/herdr-agent-ops-plugin`, then
   `herdr plugin list`.
4. Open it: `herdr plugin action invoke open --plugin herdr.agent-ops` or `herdr plugin pane open --plugin herdr.agent-ops --entrypoint map` (needs an active workspace pane).

The manifest follows the `[[panes]]`/`[[actions]]` shape in Herdr's plugin docs as summarised in
`docs/specs/2026-11-12-agent-operations-map/DISCOVERY-B-siblings.md`; `herdr plugin link` is the real validator, so
if your Herdr version rejects a field, fix it there first.

## Doctor

- `herdr plugin action invoke doctor --plugin herdr.agent-ops` (or `herdr-eng agent-ops --once`) prints one frame; a non-zero
  exit with a JSON error means the URL, key or the `agent_ops_v1` capability is wrong (`GET /api/v1/health` lists it
  under `capabilities`; 404 on `/api/v1/agent-ops/snapshot` means it is disabled).
- `herdr plugin log list` shows plugin command output.
- Keys: Tab pane, arrows move between agents, Enter inspect, h handoffs, t timeline, r replay (`[` `]` step, `{` `}`
  jump 10, `,` start, space pause/play, `l` live), f filter edge kind, m map mode, ? help, q quit.
- Accessibility: `NO_COLOR`, a `TERM` without color/Unicode, `--ascii`, and `HERDR_REDUCED_MOTION=1` or
  `--no-animation` are honoured (pulses become static).
