# Live UI refresh and GitHub cache — design

Status: draft for approval. Date: 2026-10-24.

## Intent

The web UI re-renders too much and too visibly. Five routes (`/hosts`, `/hosts/:id`,
`/activity`, `/analytics*`, non-Pi `/missions/:id`) replace the whole page with a spinner on
every throttled SSE event (`app.js:114-136`, `app.js:591-603`), losing scroll, open `<details>`,
filters and selection. The SSE stream also closes after ~15 s idle and replays 100 events on
reconnect (`control_plane/api.py` `_sse`), firing about 100 spurious refreshes. Every other view
polls and swaps whole panels with its own copy of a signature/restore routine, with no motion.

Separately, `GitHubMonitor` (`herdr_engineering/github.py`) re-reads everything every 300 s:
all repositories, then every open issue of every repository, 100 per GraphQL page, held only in
memory. A restart refetches all of it. No change detection, no ETag, no persistence. GitHub
Projects are not read at all.

The operator wants: components that update in place and look good doing it; a plan covering
all pages; and GitHub issues/projects fetched incrementally ("only fetch new things").

## Goals

1. No page-level re-render or spinner after first load. Only changed elements update.
2. One shared keyed patcher replaces the nine hand-rolled signature/restore copies.
3. A small motion language (tween, ease, flash, pulse, enter/exit, skeleton, draw-in) built on
   the existing `--motion-*` tokens; honours `prefers-reduced-motion`.
4. Each page shows a freshness chip ("live · updated 3 s ago"; amber when stale; error state).
5. Spurious SSE refreshes stop (server keepalive; client ignores connect frame and replay).
6. GitHub issues sync incrementally: steady-state cost is proportional to what changed, not to
   catalog size; survives restarts; closed/deleted issues are still detected.
7. Per-page visual/dynamic upgrades listed under "Per-page".

## Non-goals

- No framework or chart library; hand-rolled SVG and plain DOM stay.
- No webhooks (no public endpoint to receive them).
- No change to what data any page shows, except GitHub Projects (phase G5, needs confirmation).
- No redesign of layout or information architecture.

## Anti-goals

- Motion on unchanged data, or animation that delays reading data.
- A page that looks live but is silently stale (the chip must go amber/red).
- A cache that serves a closed issue as open, or hides a sync failure.

## Design: frontend

**Patcher** (`packages/patch.js`, no deps): `patchList(container, items, {key, render, update})`
keyed on a stable id: existing nodes updated in place, new nodes inserted and marked
`data-entering`, removed nodes marked `data-leaving` and removed on `transitionend` (or at once
under reduced motion). `patchText(el, value)` / `patchAttr` write only on change. The patcher
never touches a node the user is interacting with (focused input, open `<details>`, text selection).

**Router rule:** `render()` runs only on a route change. Same-route refresh is `view.refresh()`.
The five full-render routes get a `mount` returning `{refresh, dispose}` (as Overview does). The
spinner shows on first load only. Host-detail `?range=` becomes an in-page tab that swaps series.

**Stable keys** (most exist): add `data-host-id` on host cards and fleet-matrix cells; select
Activity rows by `event_id`, not array index.

**Refresh scheduling:** one shared trailing-throttle (5 s) driven by SSE event type as a hint
(`HostHeartbeat` -> that host's gauges; `MissionStageChanged` -> that mission row; unknown -> the
view's `refresh()`), plus the existing per-view polls as fallback. Hidden tab pauses; becoming
visible refreshes once.

**SSE:** server emits `: keep-alive` every ~10 s instead of ending on idle. Client skips
`StreamConnected` and events whose `sequence` it has already seen (replay).

**Freshness chip** (`<herdr-freshness>`): last successful refresh time, ticks every second,
amber past 2x the view's interval, red on error with the message.

**Motion** (`packages/motion.css`, `packages/motion.js`), all durations from `--motion-fast`
(120 ms) / `--motion-normal` (200 ms): number tween ~300 ms (JS, checks `matchMedia` for reduced
motion); `transition: width` on meters/progress/stage bars; changed-value flash (`--accent-weak`
fade ~600 ms); row enter/exit (height+opacity); status pulse for running/live/critical;
sparkline draw-in on first paint only; skeleton cards replacing "Loading…" text; "N new" pill
when the user has scrolled away on Activity and the session stream.

**Foundations:** define missing tokens `--bg`, `--accent-contrast`, `--surface-alt` in
`theme.css`; add a dark theme to the tokens; add `hd-table` styles.

## Per-page

Overview: count-up on metric tiles, skeletons, pulse on new critical item, freshness chip.
Topology: keyed row diff on `data-topology-entry`, count-up, `current` status dot.
Missions: animated `<progress>`, row enter/exit by `data-mission-key`, freshness badge colour
transition, tab/filter count-up. Mission detail: non-Pi path reuses the history refresh; timeline
slide-in for new items. Sessions: keyed rows, heartbeat dot, new-row highlight. Session detail:
append transition for inspector rows (already keyed by `seq`), head status updated from SSE,
recorded-events list stops flickering. Services / Service detail: state-change flash, count-up,
"last report" tick. Hosts: meters ease, stale hosts dim with "no report for N min", sticky matrix
header, flash on drifted cell. Host detail: gauges, sparklines, top-consumer rows update in
place; outdated-packages `<details>` stays open; sparkline draw-in once. Activity: keyed prepend
with slide-in, "N new" pill, filter and selection persist. Analytics: count-up; stage bars
scale to share of total (today `n*14%`); changed-cell highlight. CI/Reviews: pulse on running
pipelines, health-cell flash on change, chart draw-in. GitHub: keyed diff + changed-issue
highlight. Collab: reuse `cx-flow-pulse`; slide-in for chat rows.

## Design: GitHub cache

Today's cost per sweep = ceil(repos/100) + sum(ceil(open_issues_r/100)) GraphQL calls, repeated
every 300 s, from empty memory after each restart.

**G1 Persistent store.** Repositories, issues, and watermarks persist in the control plane's
Postgres (new migration; SQLite fallback only if the control plane already supports it). On start
the monitor loads the store and serves it immediately as `stale` until the first successful
incremental pass; a restart never triggers a full re-read.

**G2 Cheap change probe.** The repository listing query (already paged, one call per 100 repos)
additionally returns, per repo, `issues(states:OPEN){totalCount}` (exists) and the newest
`updatedAt` via `issues(first:1,orderBy:{UPDATED_AT,DESC})`. A repo needs an issue fetch only if
its open count or newest `updatedAt` differs from the stored values. Unchanged repos cost nothing
beyond their share of the listing page.

**G3 Incremental issue fetch.** For a changed repo, page `issues(states:OPEN, orderBy UPDATED_AT
DESC)` (already the query order) and stop at the first issue whose `updatedAt` is not newer than
that repo's stored watermark; upsert what was read. New watermark = max `updatedAt` seen.

**G4 Closed / deleted / transferred reconciliation.** Open-only ordering cannot see issues that
left the open set. Per changed repo also read `issues(states:CLOSED, filterBy:{since:watermark})`
(stop at watermark) and remove or mark those closed. If the stored open count still disagrees
with GitHub's `totalCount` after G3+G4 (deletes, transfers), do a full read of that repo only.
Safety net: one full reconciliation per repo every 24 h (staggered), so drift is bounded.

**G5 Projects (needs operator confirmation).** Read-only GitHub Projects v2 via the same layer:
list the viewer's/orgs' projects (with `updatedAt`), fetch items only for projects whose
`updatedAt`/item count changed, store items keyed by item id with the same watermark and
reconciliation pattern. Scope (which projects, which fields, which page) is an open question below.

**G6 Rate-limit budget.** Keep the existing `rateLimit {remaining resetAt cost}` handling; add a
per-pass point budget (config, YAML) so a pass defers remaining repos to the next interval rather
than exhausting the quota. Issue detail cache TTL (60 s today) becomes: serve stored detail while
the issue's `updatedAt` is unchanged, refetch only when the incremental pass saw it change.

**G7 Delivery to the browser.** `/api/v1/github/*` responses carry a monotonically increasing
`revision` and `ETag`; the UI sends `If-None-Match` and gets `304` when nothing changed. Lists
accept `since=<revision>` and return only changed/removed rows, which the keyed patcher applies,
so the browser also stops refetching and re-rendering unchanged pages. The same ETag middleware
is applied to the other read-only JSON endpoints the polled views use (hosts, sessions,
services, missions) as a cheap, generic win.

**Config (YAML only, no env):** `github.sync.interval`, `github.sync.point_budget`,
`github.sync.full_reconcile_interval`, `github.projects.*`.

## Order of work (each step shippable and tested)

1. Plumbing: SSE keepalive + replay fix; patcher; freshness chip; missing tokens/dark theme.
2. Mount the five full-render routes; no spinner after first load; host range tabs in-page.
3. Motion primitives; apply first to Hosts and Overview.
4. Per-page polish, one PR per area.
5. Migrate the remaining nine views onto the patcher; delete the duplicated code.
G1-G4 (persistent incremental issue sync) can run in parallel with 1-3 since backend-only;
G6-G7 after G4; G5 after G4 and confirmation.

## Testing

Patcher: unit tests for insert/update/remove/reorder, focus and open-`<details>` preservation,
reduced-motion immediate removal. Mounted views: DOM-level tests that a refresh with identical
data does zero DOM writes and a changed row mutates only that row. SSE: keepalive frames; replayed
`sequence` ignored. GitHub: fake provider counting GraphQL calls — unchanged catalog costs only
the listing; one changed issue costs one repo's pages stopped at the watermark; closed issue
removed; deleted issue caught by the count check; restart serves the stored catalog with zero
calls before the first pass; rate-limit budget defers the rest. ETag: 304 on unchanged,
`since` returns only deltas.

## Left to the builder

Patcher internals and attribute names; exact easing curves; tween implementation; DB schema
details; whether the generic ETag middleware is a decorator or handler wrapper; sparkline draw-in
mechanics; which count of repos the staggered 24 h reconcile processes per pass.

## Calls made without the operator

- Postgres (not files) for the GitHub store: the control plane already runs it. (Telemetry stays
  in files per an earlier decision; this is catalog data the UI queries.)
- 24 h staggered full reconcile as the drift safety net.
- Dark theme tokens included in the foundations step.
- Generic ETag support on other polled endpoints, beyond GitHub.

## Open questions for the operator

1. GitHub Projects: which projects (all the token can see, or a configured list), and what should
   the page show (board by status column, or a flat item list)?
2. Is a dark theme wanted now, or is that scope creep for this effort?
3. Is "calm mode" (a visible motion toggle) wanted beyond following the OS reduced-motion setting?

## Builder-check rulings (2026-10-24)

A fresh reader asked eight questions; none needs the operator. Rulings, each a call by the author:

1. **Revision scope.** One monotonically increasing integer per resource family (GitHub catalog is
   one family), stored in Postgres; each row keeps the revision of its last change. Removals are
   kept as tombstones for 7 days; a `since` older than the oldest tombstone gets `{"resync": true}`
   plus the full list. The generic ETag on hosts/sessions/services/missions is a content hash of
   the serialized body: it saves bandwidth and client re-render, not server work (stated plainly).
2. **Token identity.** Stored rows and watermarks are tagged with the existing credential
   fingerprint (`credential_identity`) and viewer login. On mismatch the catalog is dropped, never
   served; revoked access cannot leak private issues.
3. **G2 probe is unproven.** A spike task (G0) runs first against the real token: measures listing
   cost with the extra `issues(first:1,orderBy UPDATED_AT)` field at 100 repos/page, and confirms
   comments, label changes and edits bump `updatedAt`. G2-G4 are gated on its result. Fallback:
   smaller listing pages (50) or aliased per-repo probes batched into one query.
4. **Process model.** The monitor lives only in `control-plane-web` (one replica; the worker never
   starts it). A Postgres advisory lock still wraps each sync pass. The web container has no
   writable volume and the monitor is synchronous/threaded, so G1 uses a small synchronous Postgres
   adapter on the existing `control_plane.pg_dsn`; G0 confirms which driver is already installed
   and, if none fits, falls back to SQLite on a new named volume.
5. **Detail/comments.** Comments are not fetched today and stay out. Issue bodies are stored only
   for issues whose detail was opened (a detail table), refetched when the incremental pass sees
   that issue's `updatedAt` change.
6. **Sync state for the chip.** The existing snapshot fields (`status`, `stale`, `partial`,
   `last_success_at`, `progress`, `rate_limit`) feed the chip; add `deferred_repos` so a repo the
   point budget keeps deferring shows as stale instead of silently old.
7. **Test harness.** Pure logic uses `node:test`. DOM behaviour (zero writes on identical data,
   focus/`<details>` preservation) uses Playwright, already in the repo (`tests/playwright`); the
   first task adds a fixture-page harness. No jsdom is added.
8. **SSE proxying.** Keepalive every 10 s plus `X-Accel-Buffering: no`; verified live on bender
   (via `dev.lan`) in step 1, since that is the real path.
