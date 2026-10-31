# P06 — Team-first WebUI and persistent visual map: result (partial)

Status: PARTIAL. The Team Map screen for one project is built; "Your teams" home and the Conversation view are not.

## Built (merged state: see the PR that added this file)
- `packages/team-map-model.js`: the Team Map's logic with no DOM. Groups a Work Graph snapshot project -> issue -> task ->
  deliverables (via `contains` edges, deliverables under what `produced` them), folds noisy execution kinds into a count,
  filters by kind/status/text, maps every edge type to one of the five meanings (coordination, delegation, dependency,
  produced, reviewed), describes the whole map as sentences (the text fallback; historical edges marked "no longer current"),
  gives progress as milestone counts ("1 of 2 tasks done") and never a percentage, and lays nodes out deterministically
  without moving anything already placed (personal > shared > automatic, per node).
- `HerdrClient.workGraphSnapshot / workGraphLayout / saveWorkGraphLayout`: the existing P02 routes, for the view.
- Tests: `packages/herdr-web-components/team-map-model.test.js` (8, including a 4,001-node graph grouped, described and laid out
  under 1.5 s) and one client test.

## Built: the Team Map screen for one project
- `packages/team-map-view.js` + `packages/team-map.css`, route `#/team-map?root=project:<id>` (nav entry "Team map" under Agents, shown only when `/api/v1/health` advertises `unified_workspace_v1`; the route itself is registered in `packages/app.js`, stylesheet linked in `packages/index.html`).
- Renders from `team-map-model.js` unchanged: an SVG picture (columns project / issue / task / agent / deliverable; five line meanings told apart by dash pattern and a legend, earlier relations faded and labelled "no longer current"), milestone counts per project ("1 of 2 tasks done", never a percentage), filters (search, kind, status, show agent sessions and hosts with the folded-away count), an inspector with a link to the existing artifact/session/host/review page, and the written map (the model's text fallback, open by default) that lists everything, including what the picture caps at 300 nodes.
- Keyboard: one tab stop; arrow keys / Home / End walk the nodes; Enter or Space opens details; Alt+arrow moves a node one cell (refused onto another node or off the map). Every node has an `aria-label`; focus ring from `--focus-ring`; targets 44px; colours only from design tokens.
- Layout: shared and personal positions are read with `workGraphLayout`; new nodes are placed incrementally and nothing already placed (saved or drawn earlier in the session) moves. "Save my layout" writes the personal layout through `saveWorkGraphLayout` with the ETag: saved entries are kept verbatim, only moved nodes change, nodes with no saved position are stored where drawn; a stale ETag (412) is refused with "reload, then save again". Shared-default editing has no UI.
- States: not turned on / project not found (the graph routes answer 404 for both; plain message, not an error, and it does not mark the page "unavailable"), 503 unavailable, error, empty project, filters match nothing, truncated snapshot, layout unreadable (map shown, saving hidden).
- Tests: `packages/team-map-view.test.js` (14): real `team-map-model.js`, real `HerdrClient` against a real local HTTP server speaking the graph routes' contract (404 when off, ETag/If-Match on the layout). They cover markup, escaping, filters, caps, placement, moves, the save document, ETag conflicts, the off state.

## Not built (truthfully)
- Still not built: the "Your teams" home, the Conversation view, deep links from other pages into the map. There is no `team` entity producer yet outside tests, so a "Your teams" list has nothing to read in production. The map is opened by typing a project id.
- Not exercised in a browser: no Playwright is installed where this was built, and no browser test was added or run. The SVG, focus ring, keyboard handling in `mountTeamMap`, layout at phone width and the ui-gates (a11y, keyboard, visual baselines, perf) are covered only by CI. `ui-gates.yaml` floors and baselines were not touched. Whether a novice completes a workflow unaided is not shown.
- Not built (original list, still true): the Conversation view and "Your teams" home; gate "a novice completes one workflow with no documentation" and "graph/timeline/gallery agree on artifacts" need those.
- Gate not met: see above.
- Rollback: the page only appears while `control_plane.unified_workspace.enabled` is true (nav entry by capability; the graph routes 404 otherwise). To remove: delete `team-map-view.js`, `team-map.css`, their test, and the import / nav / route lines in `app.js` and the stylesheet link in `index.html`.
