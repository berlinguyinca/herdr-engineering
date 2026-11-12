# Handoff: slice 06, terminal Agent Operations map

Branch `feat/agent-ops-tui`. Commits (prefix `agent-ops-tui:`):
- `2d644c6e` renderer, key state machine, SSE/HTTP client, `herdr-eng agent-ops` registration, tests
- `666cdae2` Herdr plugin manifest + README, real-server captures (text/PNG), overlay-scroll fix, pty e2e test
- a final commit adds context-bar colouring, the captures refresh, and this document (see `git log`).

## What shipped
- `herdr_engineering/agent_ops_tui.py` (stdlib only); `cli.py` +3 lines (`register`). Run `herdr-eng agent-ops
  [--url --api-key --token --ascii --no-animation --once --width --height]`; env `HERDR_CP_URL`, `HERDR_CP_API_KEY`,
  `HERDR_COLLAB_TOKEN`, `NO_COLOR`, `HERDR_REDUCED_MOTION`.
- `render(snapshot, width, height, mode, focus, tick, opts)` is pure and total: always exactly `height` lines, none wider than
  `width`, and an internal failure becomes a `render error` message rather than an exception. Layouts: full (>=100x30:
  map hero + Tasks/Agents/Activity panels + Timeline), compact (80x24: map + Tasks/Activity strip), list (<60 cols).
- Edges: per-kind colour + line glyph + arrow (delegation heavy green, handoff yellow `⇒`, review `◇`, communication blue,
  ownership `●`), dashed for planned states (`planned|proposed|pending|queued|requested`), labels, parallel lanes, pulse on
  active edges (a moving `◉`; static at the midpoint under reduced motion). Unknown model is `model ?`, unknown context `ctx ?`.
- `handle_key(UIState, key, agent_count, event_total)` pure reducer; effects (`quit`, `("replay", i)`, `live`) executed by `run`.
- Client: snapshot poll every 5s, SSE `/stream` with `Last-Event-ID` resume and 1s..30s backoff; status
  `LIVE|DEGRADED|OFFLINE|REPLAY`; replay fetches `/events` pages for cursors then `/replay?until=`.
- Labels pass through `clean()` (control chars stripped, bounded); ASCII mode transliterates every non-ASCII glyph.
- Plugin: `deploy/herdr-agent-ops-plugin/` (manifest with `[[panes]]` split x2 + `[[actions]]` open/doctor, README).
  Not linked into the running Herdr.

## Tests (`/private/tmp/claude-501/aoenv/bin/python -m pytest tests/test_agent_ops_tui.py -q -p no:cacheprovider`)
27 passed: render at 120x40/80x24/50x20/30x8 (fit, content, ASCII/unicode/color), `ctx ?`, planned vs accepted glyphs, ASCII
fallback, control-char stripping, NO_COLOR/TERM/reduced-motion detection, pulses animate vs static, modes/filters, overlays,
status line, garbage snapshots, 14-agent grid, key state machine and replay flow, key parsing, and real-server tests: render
of the real snapshot + `/replay`, SSE stream and backoff against a dead port, and a real pty session driving the actual
`herdr-eng agent-ops` process (h, ?, r, [, l, q) against the real server. No mocks.

## Comparison with reference 03 and iteration
Reference: glowing ring of circular nodes around a room hub, ctx rings, labelled callouts, right-hand Tasks/Activity/
Agent-status panels, timeline strip, key footer. Ours keeps the information architecture (map hero, Tasks, Agents,
Activity, Timeline, key footer, status) in character cells. Differences by necessity: boxes instead of circles, no glow,
no provider icons, ctx as a bar (amber >=70%, red >=90%) not a ring.
Polish pass (after review of the first captures): 3-8 agents now sit on an ellipse (top/right/bottom/left) around a central
hub drawn as a dotted box (`room …<id tail>` + member count from `rooms[]`, else `ops hub`); 1-2 agents or panes too small
for the ring use the even grid. Edges are routed by a small Dijkstra over free cells (few turns, never through nodes or the
hub, heavy cost for running along another edge, cheap crossings), so no long shared trunks. Labels spell the kind
(`delegate · api-impl`, `~review · …` when planned) and only the task id shrinks; they sit beside the longest clear run.
Focus is a reverse-video name row plus `▸`; the Agents panel columns fit `attention` and 10-char models; compact nodes
abbreviate `attention` to `attn`. The pulse `◉` travels the active edge each tick (static mid-edge under reduced motion).
Capture now seeds a real collab room (alice token) so the hub header is in the goldens and in a real-server test.

Honest gap vs reference 03: boxes not circles, no glow/ctx rings/icons; opposite nodes (top-bottom) still need a long
edge past the hub, and edges can run close to node borders at 80x24. Edge geometry is good but not as polished as the mock.

## Limitations
- Click/double-click-to-open-pane and Herdr pane sync are not implemented (no socket).
- Routing is a per-edge greedy search (order-dependent); 9+ agents fall back to the scrolling grid, where edges can crowd.
- Filters are edge-kind only (room/host/model/time filters from the spec are not in the TUI); the timeline bar is derived
  from edge/model-change timestamps in the snapshot, not from raw events.
- Replay play-through steps one event per tick (0.25s) while unpaused; speed control is not implemented.
- PNGs use Menlo in Chromium; real terminals may differ for `◉ ◇ ⇒`. Plugin manifest pane fields are unvalidated
  against a live Herdr (`herdr plugin link` is the validator; I did not run it).
- API key alone shows no rooms (backend rule); pass `--token`.
