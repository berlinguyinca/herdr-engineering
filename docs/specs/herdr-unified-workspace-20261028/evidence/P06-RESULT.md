# P06 — Team-first WebUI and persistent visual map: result (partial)

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

## Not built (truthfully)
- The screens: "Your teams" home, the Conversation view, the Team Map view (picture), the inspector and deep links. There is no
  `team` entity producer yet outside tests, so a "Your teams" list has nothing to read in production.
- Gate not met: "a novice completes one workflow with no documentation" and "graph/timeline/gallery agree on artifacts" need the
  screens.
- Rollback: nothing is wired into a page yet, so there is nothing to roll back; deleting the two files removes it.
