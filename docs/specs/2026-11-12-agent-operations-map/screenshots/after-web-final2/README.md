# After screenshots (final): web Agent Operations map with a populated room

Real captures of `#/agent-ops` from a real control-plane server. Not mock-ups, nothing stubbed.

Regenerate:

    node tests/playwright/agent-ops-shots.mjs docs/specs/2026-11-12-agent-operations-map/screenshots/after-web-final2

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

## What changed since after-web-final (gaps 1 to 5 of the brief)

1. Fit: the SVG takes its aspect ratio from the scene viewBox, the scene is laid out for the card width (wide ellipse on desktop/tablet, a two-column rounded box under 560 px), so the map fills the card. Nodes are larger (r 52), text is 11.5 to 15 px at 1440 (was 7 to 8). At 390 nothing is clipped; names are truncated to 9 characters (full text in aria-label and inspector).
2. The person node sits inside the room it takes part in (room taken from its edges). The Room filter drops persons whose edges belong to other rooms.
3. Edge labels are collision-checked against each other, node rings and node captions; a label with no free spot is hidden until hover/focus/selection (legend and aria-label still carry it). On desktop "Handoff - completed / stalled / failed" are separate lines of text now.
4. Room title and Room filter use the room name ("product-dev"; unknown name falls back to a shortened id). Backend: additive `rooms[].name` in the snapshot and replay (readable rooms only, else null).
5. Drift: warning-triangle badge on the node ring (title and aria say "heuristic"). Model switch: a "switched from X" pill under the node, separate from handoff edges. Both are in the legend.

## Honest problems still visible

- Phone 390: node text is about 13 to 15 px after scaling, but with 7 nodes and 8 edges the lines cross heavily and most edge labels are hidden (shown on tap/focus). Names are truncated to 9 characters.
- Desktop: a few labels still sit on top of another edge's line (e.g. "Delegation - accepted" near the Qwen node); there is no routing, only label placement.
- Names inside nodes are cut at 11 characters ("Claude (Pl...").
- The model line is the current model; the "switched from" pill names only the previous one.
- Tasks without `owner.transferred` read "owner not assigned" (correct for the seed, sparse looking).
- The "Attention" nav badge changes while the page is open (19 in later captures).
- Console: occasional `503` from the stream endpoint (2 concurrent streams per caller; the capture script reloads until the paused banner is gone).

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
- Nodes are now large and readable on desktop (the largest gap before); the references still have glowing provider-glyph rings, floating legend and edge callout cards, which we do not.

### Remaining gap per reference after this pass
- 01: still missing page chrome, gantt timeline, right-hand Current work / Room health column, edge callout cards. Person-in-room, room name, ring size and drift marker now match in substance.
- 02: person is now inside the room; still no Messages panel (by design), Room State, Recent Activity, Task Lineage tab, cross-room link node, 0:00 / 4:12 clock.
- 03: still no Agent Status table, rooms list or activity log next to the map.
- 04: still no Live/Room/Fleet toggle, task cards, bottom KPI tiles, floating legend.
