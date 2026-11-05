# Work Command Center: operator guide (WC-001 to WC-006)

Status: code complete on branch `feat/wc001-s6-validation-rollout`; **nothing is deployed**. No production coverage has been observed; everything below describes what the code does, verified against fixtures and the fixture server only.

## Two-minute walkthrough
1. Open the dashboard Overview (`#/overview`). Under Attention is the **This week** card: five tiles (Started, Finished, Open, Quota-paused, Needs attention) and rows grouped Active / Paused / Completed this week / Unconfirmed.
2. Click a row. A mission row opens the existing mission page (`#/missions/<key>`); an agent that belongs to no confirmed mission opens its session (`#/herd/session/<id>`). One click.
3. On a session page the **Work context** header (above the story) names the parent mission, its measured progress and the other agents of that mission. "Not linked to a mission" means the association could not be proven by ID, not that none exists.
4. The **Terminal** button on the session page shows a copyable `herdr attach ...` command. The browser never opens or focuses a terminal.
5. On **Projects** (`#/github/projects`), a PR card shows **Open in Herdr** only when exactly one mission recorded that exact repository and PR number. Otherwise the card has only its GitHub link.

## Enable, disable, roll back
- Capability name: `work_center_v1`, advertised in `GET /api/v1/health` `capabilities`.
- Enable: set `control_plane.work_center.enabled: true` in the control-plane YAML and restart the control plane. Strict boolean `true` only (`"true"`, `1`, a missing key all mean off). Default is **off**.
- Disable (rollback, no redeploy): set it to `false` or remove the block, restart the control plane. Open dashboards pick it up on the next page load (the probe runs once per load).
- When off, the dashboard has exactly the pre-Work-Command-Center behaviour: no This week card (saved layouts ignore it), no Work context header, no Open in Herdr link, and the board does not read `/api/v1/mission-outcomes`. A failed or slow (over 3 s) capability probe also means off. Covered by `tests/playwright/work-center-e2e.test.mjs` and `work-center-real-server.test.mjs`.
- The server-side additive `prs` field on `GET /api/v1/mission-outcomes` (WC-005b) is always present; with the flag off the dashboard does not read it and no other client depends on it.
- Code rollback: revert the WC-001 to WC-006 commits (all additive; no schema, route or table was removed or changed).
- The flag does not change any permission: the same read endpoints, same credentials, no writes.

## What the numbers mean
- **Started / Finished (G1)**: one read of `GET /api/v1/work/weekly-events` per Overview load (flag on only; no polling) supplies stored started/finished events, merged by work id. The badge reads "at least N (partial) - history from <date>" where the date is the earliest stored lifecycle event (the horizon). "complete" appears only if the server says `coverage.complete:true` for the whole week; this build never does (see Known gaps). If the read fails the card falls back to the snapshot-only numbers and says so.
- **Started / Finished (snapshot fallback)**: missions with a recorded timestamp in the current Monday-to-Monday week (browser time zone). They are lower bounds: the card says "at least N (partial)" because the server has no weekly history, so work that left the snapshot cannot be counted. "unknown" with a dash means no timestamps exist at all; it is never shown as 0.
- **Open / Quota-paused / Needs attention**: snapshots of right now. Quota-paused counts only missions whose status says quota. Needs attention is blocked, in review or failed with an update this week (approximation).
- **Coverage words**: *complete* = every source loaded and nothing ambiguous; *partial* = a source is missing/failed or something was ambiguous, so the number is a lower bound; *unknown* = no evidence, no number is shown.
- **Unconfirmed** group: the report is stale, disconnected or only recorded, so the state may have changed.
- **Progress bar**: only appears when the mission reports measured evidence (acceptance criteria, else weighted DAG), through the existing `measuredProgress`. An agent finishing never completes its mission and never produces a bar or a 100%.
- A mission with many agents is one row; an agent without a provable mission is its own row marked as not linked.
- The card shows at most 5 rows per group ("Showing 5 of N").

## Known gaps
- Weekly history (G1): `GET /api/v1/work/weekly-events?since=&until=&limit=&after=` reads the in-memory event streams (same source and auth as `/api/v1/events`; 404 when the flag is off; max 31-day span, limit max 500). It cannot prove completeness: start events are written when the control plane first sees the work (not the true start), the durable Postgres sink is best-effort and not retried, and work that never ends has no finish event. So `coverage.complete` is always false here, Started/Finished stay "at least N (partial)", and the banner stays visible. Quota-paused history is not emitted by any Python code, so no paused_quota events exist yet.
- Issue cards never link to Herdr: no issue-to-mission relationship is recorded anywhere.
- A PR links only for missions that have a stored outcome (ended missions); a PR recorded by a still-running mission verifies after it ends. The board reads refs once per page load.
- The week uses the browser time zone (no user setting) and starts on Monday.
- Terminal attach is copy-command only.
- Saved Overview layouts place This week last (new cards append to a saved order) until the user moves it.
- Two recorded missions that share one id and no source are keyed together by the existing mission catalog; a Work context link to such a mission is labelled "not fully confirmed" rather than hidden.
- No next-action field and no issue/PR link inside the Work context header (no verified source exists).
- Filter chips and expanded row details from the UX reference are not built.

## Observed production coverage
None. Nothing has run against production data. After enabling, check the card on the real Overview: the Unconfirmed group and the partial banner show how much of the fleet reports current data, and the count of rows vs `Agents`/`Current missions` shows how many agents are not linked to a mission.
