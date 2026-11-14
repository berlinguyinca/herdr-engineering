# After screenshots (final): web Agent Operations map with a populated room

Real captures of `#/agent-ops` from a real control-plane server. Not mock-ups, nothing stubbed.

Regenerate:

    node tests/playwright/agent-ops-shots.mjs docs/specs/2026-11-12-agent-operations-map/screenshots/after-web-final

`tests/playwright/agent-ops-room-server.py` builds a real `CollabService` (user `alice`, project, room), starts the server with
`operator_user="alice"`, `single_operator=True`, `agent_ops_config={"enabled": True}`, and POSTs 32 envelope-v2 events to the real
`/api/v1/ingest` with `visibility_scope: "room"` and the room id. The single operator's own collab identity is what makes the room
readable (an API key alone reads no rooms). No product code was changed; `unified_workspace_v1` was not needed.
`OFF=1` starts the same server with agent_ops disabled.

## What the seed contains (and what the screens show)

| Item | Seeded as | Visible |
|---|---|---|
| Room cluster | all events carry the room id | yes: one dashed circle around six agents, label "ROOM ROOM_0...RZGBJ - AVG 72% CTX" |
| Pi / Claude / Codex-shaped sources | sources `pi`, `claude-code`, `codex` | yes (provider and model on nodes: qwen3-72b, claude-opus-5, gpt-5.5) |
| Ownership edge vs delegation edge | `owner.transferred` Claude to Qwen; `delegation.accepted` Claude to Qwen; `delegation.proposed` Claude to Codex | yes: teal "Ownership - transferred" vs violet "Delegation - planned" (dashed) |
| Completed model handoff | `handoff.state completed` Codex to Opus, then `model.switched` on Opus | edge "Handoff - completed" yes; the model switch is only visible as Opus reading `claude-sonne...` (no "switched from" marker on the node) |
| Stalled handoff | in-flight handoff to Gem, then Gem disconnected | yes: dash-dot orange, "Handoff - stalled", inspector text |
| Failed handoff | in-flight handoff to Lint bot, then its session ended | yes: dotted, "Handoff - failed" |
| Context amber / red / unknown | Codex 78%, Claude 92%, Opus / Gem / Lint none | yes: amber ring + "78% ctx", red ring + "92% ctx !", "ctx unknown" |
| Drift warning | `drift.warning` on Qwen; 3 consecutive `tool.failed` on Codex | yes in inspectors ("Drift signals (heuristic)"); no badge on the map node itself |
| Hosts strip | mac-01 (explicit reconnect = connected), dev-02, dev-03, dev-04 (no link state = unknown) | yes: five chips incl. "Fleet" aggregate |

## Files

`map-{light,dark}-{1440,820,390}.png`, `map-replay-light-1440.png` (scrubbed to event 18 of 32), `map-reduced-motion-light-1440.png`,
`room-collaboration-light-1440.png` (Communication mode with the room filter set),
`inspector-ownership-task-{light,dark}-1440.png` (Task T-428: owner / contributors / reviewer / drift),
`inspector-ownership-edge-{light,dark}-1440.png`,
`inspector-handoff-{stalled,failed,completed}-{light,dark}-1440.png`, `inspector-agent-codex-light-1440.png`.

## Honest problems seen in the screenshots (product, not capture)

- Phone 390: the map is clipped on the right; Codex and Gem are cut off at the edge of the map card. At 820 and 1440 it fits.
- The map is small inside a large canvas: node text is about 7 to 8 px at 1440, unreadable at 390. The person node ("alice") is a
  separate "No room" cluster far below the room, joined by a long line that crosses the room (a person has no room id).
- Edge labels collide in the middle of the room ("Handoff - stalled" / "Handoff - completed" overlap).
- The room is titled by its raw id ("ROOM_0...RZGBJ"), not its name ("product-dev"); the snapshot carries no room name.
- Tasks without an `owner.transferred` read "owner not assigned" (T-429..T-431); this is correct for the data, but looks sparse.
- The Room filter option is also the raw id, and selecting it does not hide the person node.
- The "Attention" nav badge changed from 1 to 19 while the page was open (it counts live attention items from the same data);
  captures taken later show 19.
- Console: occasional `503` from the stream endpoint. The server allows 2 concurrent streams per caller; consecutive captures by the
  same operator exceeded it while earlier pages' streams were lingering. The capture script waits and reloads until the
  "Live updates are paused" banner is gone, so no banner appears in these PNGs. A real second browser tab by the same operator
  would hit the same limit.

## Gap list per reference image (docs/specs/.../herdr-agent-operations-spec/references/)

### 01-workspace-overview.jpg (room page: header, avatars, tabs, big ring map, Execution Timeline, right-side Current work / Ownership & contributors / Drift / Room health)
- Missing: the room page chrome (breadcrumb, room title with agent/task/review chips, Invite, avatar stack), the sidebar room list, "Work Command Center" count.
- Missing: Execution Timeline as a gantt of per-agent bars (we have a single scrubber and a one-line event caption).
- Missing: right column with Current work (progress per task), a persistent Ownership and contributors panel, a drift card with Resolve/Details, Room health donuts (82% ctx, uptime, blocked tasks). We show these only inside the inspector after a click, and drift only as text.
- Different: agents are small flat circles with a thin ring, not large glowing rings with a provider glyph, role caption and a big "92% ctx". Edge callouts ("Claude to Qwen, Delegate: implement API" with a status check) are tiny inline labels, not cards.
- Matches in substance: amber/red ring semantics, ctx percent on the node, directional edges with distinct kinds, metadata-only inspector.

### 02-room-collaboration.jpg (room view with Activity Map / Ownership / Communication / Handoffs / Timeline tabs, Room State card, Recent Activity, Messages panel, Task Lineage, Replay controls)
- Matches: the tab set (Activity, Communication, Handoffs, Ownership, Replay), planned edges dashed, a person node at the edge.
- Missing: the Messages / Agent / Session panel (by design: no message content is stored), Room State card, Recent Activity feed, Task Lineage tree in this page (it exists only as the Overview card when work_center_v1 is on), cross-room link node ("product (Room)"), Live / Replay toggle in the header, play/step/speed with time readout 0:00 / 4:12 (we have Play, Speed, scrubber, event N / M).
- Different: the person is detached in its own "No room" cluster; the reference places "You" inside the room with an instruction callout. Room is titled by id, not "engineering".

### 03-terminal-map.jpg (terminal: header, left pane list/filters, map with ring nodes, task/activity/agent-status panels, timeline, key hints)
- Web screenshots here do not claim to match; the terminal output is in `../after-terminal/`. The shared gaps: the reference shows all panels at once (Tasks, Activity, Agent Status table with model/provider/host/ctx, Rooms list with counts, filter checkboxes) on one screen; our web page puts model/provider/host/ctx in the inspector and hosts strip, and has no activity log list or rooms list.
- The Agent Status table (agent, model/provider, host, ctx %) is the most useful missing element for scanning, because on the web map the same data is 7 px text.

### 04-agent-operations.jpg (dashboard page: Rooms selector, Live/Room/Fleet view toggles, legend card, Work Command Center / Room Details tabs, task cards, bottom KPI tiles)
- Matches: tabs Activity Map / Handoffs / Communication / Ownership / Replay, filters (Room, Task, Host, Provider, Model, Kind), legend of edge kinds, hosts strip (reference: "Hosts 4 online"), Live button and scrubber.
- Missing: Live View / Room View / Fleet View toggle, rooms dropdown with live/idle dots, right panel with task cards (progress, tags), Room Activity (Live) list, bottom KPI tiles (Room Health, Task Progress, Active Agents, Handoffs 24h, Messages 24h, Context Health, Hosts online), light/dark and fullscreen controls in the page header.
- Different: the legend sits under the map instead of floating on it; the map occupies far less of its canvas than the reference's full-bleed orbit.
- In all four references the nodes are large and readable at a glance; this is the largest single gap in our screenshots.
