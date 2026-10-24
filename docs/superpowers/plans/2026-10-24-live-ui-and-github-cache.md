# Live UI Refresh and GitHub Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pages update only the components that changed, with calm, pleasant motion; GitHub issues and projects sync incrementally from a persistent cache scoped to chosen organizations.

**Architecture:** Frontend gets one keyed patcher, a motion layer (with calm mode and dark theme) and mounted views for the five full-render routes. Backend gets an SSE keepalive/replay fix, ETag + `since` deltas, and a Postgres-backed incremental `GitHubMonitor` with an org allowlist.

**Tech Stack:** Vanilla ES modules in `packages/`, `node:test` + Playwright (existing `tests/playwright` fixture style), Python 3 stdlib control plane (`herdr_engineering/`), Postgres via the existing `control_plane.pg_dsn`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-24-live-ui-and-github-cache-design.md` (read it; it holds the operator decisions and builder-check rulings, which win over this plan on conflict).

## Global Constraints

- No framework, no chart library, no jsdom; hand-rolled DOM and SVG. DOM behaviour is tested with Playwright (pattern: `tests/playwright/github.test.mjs`: in-test HTTP server over `packages/`, chromium, `page.clock.install()`).
- Motion durations come from `--motion-fast` (120ms) / `--motion-normal` (200ms); number tween about 300ms; flash about 600ms. Calm mode (`data-calm` on `<html>`) and `prefers-reduced-motion` disable every tween, flash, pulse and enter/exit animation; state changes apply instantly.
- Configuration is YAML only; no new environment variables. Keys: `github.organizations`, `github.sync.interval`, `github.sync.point_budget`, `github.sync.full_reconcile_interval`, `github.projects.*`.
- Never serve GitHub data cached under a different token fingerprint or viewer; never hide a sync failure (stale/partial/deferred are surfaced).
- A refresh with identical data performs zero DOM writes. The patcher never touches a focused input, an open `<details>`, or a text selection.
- Existing tests keep passing; add tests with each task. Run: `npm run test:client` (logic), `npm run test:ui` (Playwright), `python3 -m pytest tests -q -k <area>` (Python).
- Deploys are the operator's; this plan stops at merged PRs. One PR per task group named in "PR grouping".

## Review Focus

- A refresh that arrives while the user is typing in a filter or has a row expanded must not clobber it (patcher, Activity, GitHub search).
- An issue closed, deleted or transferred on GitHub must disappear from the cache (G3/G4), including after a restart.
- A token rotation or an org removed from `github.organizations` must purge rows; nothing from an unlisted owner may remain served.
- SSE reconnect after idle must not trigger a refresh storm; a replayed event must not re-render.
- A client whose `since` revision predates tombstone retention must get a full resync, not silent drift.
- A literal `@agent /skill` or `@file` typed to a pi agent must arrive byte-for-byte unchanged; a forged `mentions` field must be ignored; a file edited by two agents must keep both versions.
- Rate-limit exhaustion mid-pass must leave the previous cache intact and flagged stale/deferred.

## File Structure

- Create `packages/patch.js` (keyed patcher), `packages/motion.js` (tween, flash, pulse helpers, calm state), `packages/motion.css`, `packages/freshness.js` (`<herdr-freshness>`), `packages/refresh-bus.js` (shared throttle + event-type routing), `packages/theme-controls.js` (theme + calm toggles).
- Modify `packages/herdr-design-system/theme.css` (tokens, dark theme, calm), `packages/herdr-web-client/events.js` (replay/connect filtering), `packages/app.js` (router rule, mounts), `packages/index.html` (motion.css, controls), `herdr_engineering/control_plane/api.py` (SSE keepalive, ETag, GitHub routes), `herdr_engineering/github.py` (incremental monitor).
- Create `packages/hosts-view.js`, `packages/host-detail-view.js`, `packages/activity-view.js`, `packages/analytics-view.js` (mounted views extracted from `app.js`; `renderHostDetail`/`renderHostCard` stay in `host-view.js`).
- Create `herdr_engineering/control_plane/github_store.py` (persistence), `migrations` file per existing migrations convention (find with `git grep -n "0005_event_id_per_actor"`).
- Tests: `tests/playwright/patch.test.mjs`, `tests/playwright/live-hosts.test.mjs`, `packages/herdr-web-components/motion.test.js`, `tests/test_sse_keepalive.py`, `tests/test_github_incremental.py`, `tests/test_github_store.py`, `tests/test_etag.py`.

## PR grouping

PR-A Tasks 1-5 (plumbing). PR-B Tasks 6-9 (five routes). PR-C Tasks 10-12 (motion, themes, calm). PR-D Tasks 13-18 (per-page polish, may split per area). PR-E Task 19 (migrate remaining views). PR-G1 Tasks G0-G4. PR-G2 Tasks G5-G7. PR-G3 Tasks G8-G9 (Projects, UI). PR-C1 Tasks C0-C3 (addressing, completion engine, chips). PR-C2 Tasks C4-C5 (issue refs, room files; C4 needs G2+G4 merged). PR-C3 Tasks C6-C7 (room commands, GitHub write gate).

---

### Task 1: Playwright fixture harness and patcher

**Files:**
- Create: `packages/patch.js`, `tests/playwright/patch.test.mjs`, `tests/playwright/helpers/fixture.mjs` (shared server+browser helper extracted from `github.test.mjs`'s pattern; leave existing tests untouched)

**Interfaces:**
- Produces: `patchList(container: Element, items: any[], opts: {key:(item)=>string, render:(item)=>string|Element, update?:(el:Element,item)=>void, animate?:boolean}): void`; `patchText(el: Element, value: string): boolean` (returns true if it wrote); `patchAttr(el, name, value): boolean`; `withPage(run: (page, baseUrl)=>Promise<void>)` in the helper.

- [ ] **Step 1: Write failing tests** in `patch.test.mjs`: (a) `patchList` inserts new keys with `data-entering`, updates changed items in place keeping node identity, removes missing keys (immediately when `animate:false`, after `transitionend` with `data-leaving` otherwise), and reorders by key without recreating nodes; (b) identical items cause zero DOM mutations (count with `MutationObserver`); (c) a focused `<input>` inside an item and an open `<details>` survive an update of that item; (d) `patchText`/`patchAttr` return false and do not write when the value is unchanged.
- [ ] **Step 2: Run** `npm run test:ui -- tests/playwright/patch.test.mjs`; expect FAIL (module missing).
- [ ] **Step 3: Implement** `packages/patch.js` per the Interfaces. Preserve nodes by `data-key`; skip any child that `contains(document.activeElement)` or is an open `<details>` or holds the selection; honour `html[data-calm]` / reduced-motion by removing leaving nodes immediately.
- [ ] **Step 4: Run** the test again; expect PASS.
- [ ] **Step 5: Commit** `feat(ui): keyed patcher and Playwright fixture helper`.

### Task 2: Calm state and motion core (no visuals yet)

**Files:**
- Create: `packages/motion.js`, `packages/herdr-web-components/motion.test.js`

**Interfaces:**
- Produces: `isCalm(): boolean` (true if `document.documentElement.dataset.calm==='on'` or `matchMedia('(prefers-reduced-motion: reduce)').matches`); `setCalm(mode: 'auto'|'on'|'off'): void` (writes `data-calm`, stores in `localStorage` key `herdr.calm`, wrapped in try/catch); `loadCalm(): void` (read before first paint); `tween(el: Element, from: number, to: number, {format, duration=300}): void` (instant under calm); `flash(el: Element): void` (adds `data-flash` for 600ms, no-op under calm).

- [ ] **Step 1: Write failing tests** (node:test with a minimal stub of `document`/`matchMedia`/`localStorage` injected via parameters or globalThis): `isCalm` precedence (explicit on/off beats OS), `setCalm` persists and survives a throwing `localStorage`, `tween` under calm writes the final value synchronously with no rAF, `flash` no-ops under calm.
- [ ] **Step 2: Run** `npm run test:client`; expect FAIL.
- [ ] **Step 3: Implement** the four functions to the Interfaces; `tween` uses `requestAnimationFrame` and writes only when the formatted text changes.
- [ ] **Step 4: Run**; expect PASS.
- [ ] **Step 5: Commit** `feat(ui): calm state and motion core`.

### Task 3: SSE keepalive and replay filtering

**Files:**
- Modify: `herdr_engineering/control_plane/api.py` (`_sse`), `packages/herdr-web-client/events.js`
- Test: `tests/test_sse_keepalive.py`, `packages/herdr-web-client/client.test.js` (add cases)

**Interfaces:**
- Produces: server emits a `: keep-alive\n\n` comment frame about every 10 s while idle and sends header `X-Accel-Buffering: no`; the handler no longer ends on `queue.Empty`. Client `subscribe(url, onEvent)` does not call `onEvent` for `StreamConnected`, nor for any event whose `event_id` or `sequence`-per-entity it has already delivered in this page's lifetime.

- [ ] **Step 1: Write failing tests:** Python test that an idle stream (patch the idle interval to a small value) keeps the connection open and emits keep-alive frames; JS tests that `StreamConnected` is swallowed and a second delivery of the same `event_id` (simulated reconnect replay) is not forwarded.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_sse_keepalive.py -q` and `npm run test:client`; expect FAIL.
- [ ] **Step 3: Implement** the loop change (idle interval from a module constant, YAML knob not needed) and a bounded seen-set (last 500 `event_id`s) in `events.js`.
- [ ] **Step 4: Run** both; expect PASS.
- [ ] **Step 5: Commit** `fix(sse): keepalive frames; ignore connect frame and replayed events`.

### Task 4: Refresh bus

**Files:**
- Create: `packages/refresh-bus.js`
- Test: `packages/herdr-web-client/refresh-bus.test.js`

**Interfaces:**
- Consumes: `subscribe` from Task 3's `events.js`.
- Produces: `createRefreshBus({throttleMs=5000, now, setTimer}): {register(view:{refresh:(hint)=>void, interests?:string[]}): ()=>void, dispatch(event): void, pause(): void, resume(): void}`; events route to registered views whose `interests` match `event.event_type` (or all when absent) via one trailing throttle per view; `resume()` triggers one refresh of every view; the bus pauses on `document.hidden` (wired in `app.js`, Task 6).

- [ ] **Step 1: Write failing tests** with fake timers: a burst of 50 events yields one `refresh` per view per window; events outside a view's `interests` do not refresh it; `pause`/`resume` behave as specified; `register` returns an unsubscribe that stops delivery.
- [ ] **Step 2: Run** `npm run test:client`; expect FAIL. **Step 3: Implement.** **Step 4: Run**; expect PASS.
- [ ] **Step 5: Commit** `feat(ui): shared refresh bus`.

### Task 5: Freshness chip, tokens, dark theme, calm CSS

**Files:**
- Create: `packages/freshness.js`, `packages/motion.css`, `packages/theme-controls.js`
- Modify: `packages/herdr-design-system/theme.css`, `packages/index.html`
- Test: `tests/playwright/theme-freshness.test.mjs`

**Interfaces:**
- Produces: custom element `<herdr-freshness interval="5000">` with `.touch(ok: boolean, message?: string)`; text "live · updated Ns ago", amber past 2x interval, red with the message after a failed touch; ticks every second and does not re-render when text is unchanged. `theme.css` defines `--bg`, `--accent-contrast`, `--surface-alt`, a dark palette under `@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme="light"])` and again under `:root[data-theme="dark"]`, plus `hd-table` styles. `theme-controls.js` renders a light/dark/auto control and a calm on/off/auto control into the app shell header and calls `setCalm`. `motion.css` defines the `data-entering`, `data-leaving`, `data-flash`, pulse and skeleton rules and, under `html[data-calm="on"]` and the reduced-motion media query, sets all of them to `none`/`0s`.

- [ ] **Step 1: Write failing Playwright tests:** chip turns amber then red as the fake clock advances and after `touch(false,'x')`; setting `data-theme="dark"` changes the computed `--bg`; `setCalm('on')` makes `getComputedStyle` transition duration 0 on a `[data-flash]` element; no token used in any `packages/*.css|js` file is undefined (script greps `var(--…)` names against `theme.css`).
- [ ] **Step 2: Run** `npm run test:ui -- tests/playwright/theme-freshness.test.mjs`; expect FAIL. **Step 3: Implement** to the Interfaces; call `loadCalm()` and apply stored theme in an inline script in `index.html` before first paint. **Step 4: Run**; expect PASS.
- [ ] **Step 5: Commit** `feat(ui): freshness chip, missing tokens, dark theme, calm CSS`. (Close PR-A.)

### Task 6: Router rule and mounted-view contract

**Files:**
- Modify: `packages/app.js`
- Test: `tests/playwright/router.test.mjs`

**Interfaces:**
- Produces: `render()` runs only on a route change (hash path differs); a same-route trigger calls `currentView.refresh()` through the Task 4 bus; a view is `{refresh(hint), dispose()}` and may be returned from its `view*` function; the spinner appears only on first load of a route. `document.visibilitychange` calls `bus.pause()/resume()`.

- [ ] **Step 1: Write failing Playwright test** against the real `index.html` with a stub API: emit SSE-style events and assert the `#view` root element and a scrolled position are unchanged and no `herdr-spinner` is inserted after first paint; a hash change to another route does render.
- [ ] **Step 2: Run**; expect FAIL. **Step 3: Implement** by replacing the throttled `render()` block (`app.js:549-606`) with bus registration, preserving the existing Overview/Services/Mission/Session special cases as views with `refresh`. **Step 4: Run** `npm run test:ui`; expect PASS with existing tests.
- [ ] **Step 5: Commit** `feat(ui): same-route refresh never re-renders the page`.

### Task 7: Hosts and Host detail as mounted views

**Files:**
- Create: `packages/hosts-view.js`, `packages/host-detail-view.js`
- Modify: `packages/host-view.js` (add `data-host-id` on cards and fleet cells), `packages/app.js`
- Test: `tests/playwright/live-hosts.test.mjs`

**Interfaces:**
- Consumes: `patchList`, `patchText`, `createRefreshBus`, `<herdr-freshness>`.
- Produces: `mountHosts(container, client): {refresh, dispose}` and `mountHostDetail(container, client, hostId, range): {refresh, setRange, dispose}`; `interests: ['HostHeartbeat','HostRegistered','HostUnavailable']`; range tabs call `setRange` and update the hash without navigation; outdated-packages `<details>` and scroll persist.

- [ ] **Step 1: Write failing Playwright tests** (stub client returning mutable host JSON): changing one host's `cpu` mutates only that card's meter and value nodes (assert node identity of other cards and zero mutations elsewhere); an open outdated-packages `<details>` stays open; switching range keeps the page element and swaps the series without a spinner; a stale host (last_seen old) gets `data-stale`.
- [ ] **Step 2: Run**; expect FAIL. **Step 3: Implement** by extracting `viewHosts`/`viewHost` from `app.js:343-374` and driving `renderHostCard`/`renderHostDetail` sections through `patchList`/`patchText` keyed by `data-host-id` and `data-section`. **Step 4: Run**; expect PASS.
- [ ] **Step 5: Commit** `feat(ui): Hosts and Host detail update in place`.

### Task 8: Activity as a mounted view

**Files:**
- Create: `packages/activity-view.js`; Modify: `packages/app.js`
- Test: `tests/playwright/live-activity.test.mjs`

**Interfaces:**
- Produces: `mountActivity(container, client): {refresh, dispose}`; rows keyed `event_id`; selection stored by `event_id` (not index); filter persists; new events insert at the top with `data-entering`; when the user has scrolled away from the top, new events accumulate behind a "N new" pill (click scrolls to top and flushes).

- [ ] **Step 1: Write failing tests:** new event prepends without recreating existing rows; selected row stays selected when rows shift; active filter persists across refresh; scrolled-down user sees "N new" and the list does not jump.
- [ ] **Step 2: Run**; FAIL. **Step 3: Implement** (extract `viewActivity`, `app.js:376-457`). **Step 4: Run**; PASS.
- [ ] **Step 5: Commit** `feat(ui): Activity prepends in place with a new-events pill`.

### Task 9: Analytics and non-Pi Mission detail as mounted views

**Files:**
- Create: `packages/analytics-view.js`; Modify: `packages/app.js`
- Test: `tests/playwright/live-analytics.test.mjs`

**Interfaces:**
- Produces: `mountAnalytics(container, client, sub)` for `/analytics`, `/analytics/{missions,models,tokens,hosts}` with `refresh`; stage bars width = share of total (replace the `n*14%` heuristic at `app.js:528`); non-Pi `/missions/:id` reuses the Pi history `refreshHistory` path as its `refresh`.

- [ ] **Step 1: Write failing tests:** identical payload → zero DOM mutations; one changed counter mutates only its node; stage bar widths sum to 100% proportionally; non-Pi mission page never shows the spinner after load and keeps open `<details>`.
- [ ] **Step 2: Run**; FAIL. **Step 3: Implement.** **Step 4: Run** all UI tests; PASS.
- [ ] **Step 5: Commit** `feat(ui): Analytics and mission detail refresh in place`. (Close PR-B.)

### Task 10: Motion visuals on Hosts and Overview

**Files:**
- Modify: `packages/hosts-view.js`, `packages/host-detail-view.js`, `packages/overview-view.js`, `packages/motion.css`, `packages/host-view.js` (sparkline `pathLength` + draw-in once)
- Test: `tests/playwright/motion-hosts.test.mjs`

**Interfaces:**
- Consumes: `tween`, `flash`, `patchList`.
- Produces: meter bars use `transition: width var(--motion-normal)`; `.hv-value` and `.ov-metric strong` tween via `tween`; changed values `flash`; sparkline draws once on first paint (`data-drawn`); stale hosts dim with a "no report for N min" tick; fleet-matrix header is sticky and a drifted cell flashes; Overview shows skeleton cards instead of "Loading…" text and pulses `.ov-alert` when a critical item is added.

- [ ] **Step 1: Write failing tests:** value changes 10→20 with calm off reaches 20 within the animation and with `setCalm('on')` is 20 immediately; `data-flash` appears on change and not under calm; sparkline path gets `data-drawn` once and not again on refresh; skeleton present before first data.
- [ ] **Step 2-4: Run FAIL, implement, run PASS.**
- [ ] **Step 5: Commit** `feat(ui): motion on Hosts and Overview`.

### Task 11: Motion on Missions, Mission detail, Sessions, Session detail

**Files:**
- Modify: `packages/mission-catalog-view.js`, `packages/mission-live-view.js`, `packages/sessions-view.js`, `packages/session-workspace.js`, `packages/herdr-web-components/components.css`
- Test: `tests/playwright/motion-missions.test.mjs`

**Interfaces:**
- Produces: `<progress>` value eases; rows keyed `data-mission-key` / `data-session-id` enter and leave via `patchList`; `[data-freshness]` colour transitions; tab/filter counts tween; mission timeline new items slide in; session-detail head status updates from the SSE bus (no reload); `#session-stream` appends instead of replacing; new inspector rows append with a transition (keyed by `seq`).

- [ ] **Step 1: Write failing tests** per bullet (node identity preserved for unchanged rows; `#session-stream` child count grows by one without the container being replaced; head status text changes on a pushed event). **Steps 2-4** as above.
- [ ] **Step 5: Commit** `feat(ui): motion on missions and sessions`.

### Task 12: Motion on Services, Topology, CI/Reviews, GitHub, Collab

**Files:**
- Modify: `packages/services-view.js`, `packages/topology-view.js`, `packages/integrations-view.js`, `packages/ci-charts.js`, `packages/ci-health-view.js`, `packages/github-view.js`, `packages/collab-view.js`, `packages/motion.css`
- Test: `tests/playwright/motion-integrations.test.mjs` (extend existing `github.test.mjs`, `topology.test.mjs`, `ci-*.test.mjs` only where behaviour changes)

**Interfaces:**
- Produces: services/topology/GitHub rows keyed diff with changed-row highlight (`flash`) and count tweens; running CI rows pulse (`data-state="running"`); CI health cell flashes on outcome change; CI charts draw in on first paint only; Collab reuses the `cx-flow-pulse` idiom and chat rows slide in.

- [ ] **Step 1: Write failing tests** per bullet; **Steps 2-4**; keep every existing playwright test green.
- [ ] **Step 5: Commit** `feat(ui): motion on services, topology, CI, GitHub and Collab`. (Close PR-C and the visuals part of PR-D.)

### Task 13: Freshness chip on every page

**Files:**
- Modify: every mounted view above to render `<herdr-freshness>` and call `touch(ok,msg)` after each refresh; `packages/app.js`
- Test: `tests/playwright/freshness-all.test.mjs`

**Interfaces:**
- Produces: each routed page shows a chip; a failed poll turns it red with the error; GitHub's chip additionally shows `snapshot.status`, `stale`, `partial` and (after Task G7) `deferred_repos`.

- [ ] **Step 1: Write failing test** iterating all routes with a failing stub API: every route's chip becomes red. **Steps 2-4.**
- [ ] **Step 5: Commit** `feat(ui): freshness chip on every page`.

### Task 14: Analytics polish and Topology/Services extras

**Files:** Modify `packages/analytics-view.js`, `packages/topology-view.js`, `packages/services-view.js`; Test: extend `live-analytics.test.mjs`, `topology.test.mjs`.

- [ ] **Step 1: Write failing tests:** changed table cell flashes (`data-flash`) and unchanged cells do not; topology `current` entries show a status dot; service "last report" ticks without re-rendering rows.
- [ ] **Steps 2-4; Step 5: Commit** `feat(ui): analytics, topology and services extras`.

### Task 15: Skeleton states for all first loads

**Files:** Modify every view that shows "Loading…" text; `packages/motion.css`; Test: `tests/playwright/skeletons.test.mjs`.

- [ ] **Step 1: Write failing test:** with a delayed stub API each route shows a skeleton (`[data-skeleton]`) and no layout shift greater than a fixed tolerance when data arrives (compare container height before/after). **Steps 2-4; Step 5: Commit** `feat(ui): skeletons for first loads`.

### Task 16: Calm-mode and reduced-motion regression suite

**Files:** Test: `tests/playwright/calm.test.mjs`.

- [ ] **Step 1: Write failing/guarding test:** with `setCalm('on')` and separately with emulated `prefers-reduced-motion`, visit every route, push updates, and assert no element has `data-entering|data-leaving|data-flash`, no running CSS animation (`document.getAnimations().length===0`), and values change instantly. **Step 2-4;** if it already passes, keep as a guard. **Step 5: Commit** `test(ui): calm and reduced-motion guard`.

### Task 17: Dark theme visual pass

**Files:** Modify `packages/*.css` and inline `<style>` blocks that hard-code colours (find with `git grep -nE "#[0-9a-fA-F]{3,6}|rgb\(" packages`); Test: `tests/playwright/dark-theme.test.mjs`.

- [ ] **Step 1: Write failing test:** in dark mode, for every route compute text/background contrast for primary text elements and assert at least 4.5:1; no element has a white background (`rgb(255,255,255)`) except explicit images. **Steps 2-4** replace hard-coded colours with tokens. **Step 5: Commit** `feat(ui): dark theme across all views`.

### Task 18: Docs and screenshots

**Files:** Modify `packages/herdr-web-components/README.md`, `README.md` (UI section), `CHANGELOG.md`; refresh `tests/playwright/screenshots` only where the suite already writes them.

- [ ] **Step 1:** document the patcher, motion helpers, calm/dark controls, mounted-view contract, freshness chip. **Step 2: Commit** `docs: live UI conventions`. (Close PR-D.)

### Task 19: Migrate remaining views onto the patcher

**Files:** Modify `packages/overview-view.js`, `topology-view.js`, `mission-catalog-view.js`, `integrations-view.js`, `github-view.js`, `services-view.js`, `sessions-view.js`, `collab-view.js`; delete their duplicated signature/restore helpers.

**Interfaces:** Consumes `patchList`/`patchText`; produces no new API.

- [ ] **Step 1:** migrate one view per commit; before each, ensure that view's existing Playwright test passes (baseline), then replace its whole-container `innerHTML` swaps with keyed patches and delete its local focus/scroll/`<details>` restore code.
- [ ] **Step 2:** after each view run `npm run test:ui` and `npm run test:client`; expect PASS with no new test needed beyond the zero-mutations assertion added per view.
- [ ] **Step 3: Commit** `refactor(ui): <view> uses the shared patcher` (eight commits). (Close PR-E.)

---

## GitHub cache track (backend-first; Tasks G0-G9)

### Task G0: Spike — measure the probe and find the DB driver

**Files:** Create `docs/superpowers/specs/2026-10-24-github-probe-spike.md` (findings only; no product code)

- [ ] **Step 1:** Using the token the control plane already uses (read it as the control plane does; never print or write it), run read-only GraphQL checks and record: (a) cost and node usage of the repos listing at 100/page with the added `issues(first:1,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{updatedAt}}` field; (b) whether a comment, a label change and a body edit each bump the issue's `updatedAt` (test on a scratch repo the operator owns, or report "unverified" and why); (c) whether `issues(states:CLOSED, filterBy:{since})` works for the closed-since read; (d) which Postgres driver the control-plane image installs (`git grep -n "asyncpg\|psycopg" pyproject.toml requirements*`, `deploy/control-plane/Dockerfile`).
- [ ] **Step 2:** write the findings, a go/no-go on G2-G4's design, the chosen fallback if no-go, and the driver decision (psycopg sync adapter, or SQLite on a new named volume) into the spike doc. **Step 3: Commit** `docs: GitHub probe spike results`. Later G tasks follow this doc; if it contradicts a G task's text, the spike wins and the task is amended in the ledger.

### Task G1: GitHub store (persistence, token tag, revisions, tombstones)

**Files:** Create `herdr_engineering/control_plane/github_store.py`, the migration file; Test: `tests/test_github_store.py`

**Interfaces:**
- Produces: `GitHubStore(conn_factory, *, identity: str, viewer: str, now)` with `load() -> Snapshot|None` (None and rows purged when stored identity/viewer differ), `upsert_repositories(rows, scope: set[str])`, `upsert_issues(repo_id, rows, watermark: str)`, `remove_issues(repo_id, github_ids)`, `purge_owners_not_in(owners: set[str])`, `watermark(repo_id) -> str|None`, `revision() -> int`, `changes_since(rev: int) -> {"resync": bool, "upserted": [...], "removed": [...]}`, `tombstone_retention = 7 days`, `advisory_lock()` context manager around a sync pass. Every mutation bumps the family revision and stamps the row's `rev`.

- [ ] **Step 1: Write failing tests** (against an in-memory SQLite-compatible fake or a real Postgres via the project's existing test DB fixture, per G0): load after identity change purges; revision strictly increases per change; `changes_since` returns only newer rows plus tombstones; a `since` older than the oldest retained tombstone returns `resync: true`; `purge_owners_not_in` removes rows and records tombstones; advisory lock is exclusive.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_github_store.py -q`; FAIL. **Step 3: Implement** and add the migration. **Step 4: Run**; PASS.
- [ ] **Step 5: Commit** `feat(github): persistent catalog store with revisions and tombstones`.

### Task G2: Incremental monitor core (probe + watermark fetch)

**Files:** Modify `herdr_engineering/github.py`; Test: `tests/test_github_incremental.py` (reuse `tests/test_github.py` fake provider; extend it to count GraphQL calls and model issues with `updatedAt`)

**Interfaces:**
- Consumes: `GitHubStore`.
- Produces: `GitHubMonitor(..., store=None)`; repos query gains the probe field per G0's result; `_collect` becomes: load store (serve as `stale` immediately) → list repos (filtered to `github.organizations` owners before any issue work) → for each repo compare `(open_count, newest_updated_at)` with stored → fetch issue pages newest-first until `updatedAt <= watermark` → upsert and advance watermark. Unchanged repo = zero issue calls.

- [ ] **Step 1: Write failing tests:** (a) unchanged catalog costs only listing calls (assert issue-call count 0); (b) one issue edited in one repo costs exactly that repo's pages up to the watermark; (c) new issue appears; (d) restart (new monitor, same store) serves the stored catalog with zero provider calls before the first pass and marks `stale`; (e) existing `test_github.py` stays green.
- [ ] **Step 2: Run**; FAIL. **Step 3: Implement.** **Step 4: Run**; PASS.
- [ ] **Step 5: Commit** `feat(github): incremental issue sync from a persistent cache`.

### Task G3: Closed, deleted and transferred reconciliation

**Files:** Modify `herdr_engineering/github.py`; Test: extend `tests/test_github_incremental.py`

**Interfaces:**
- Produces: for each changed repo also read `issues(states:CLOSED, filterBy:{since:watermark})` (stop at watermark) and `remove_issues`; if stored open count still differs from GitHub's `totalCount` after G2+G3, full-read that repo only; a staggered full reconcile per repo every `github.sync.full_reconcile_interval` (default 24h; at most N repos per pass).

- [ ] **Step 1: Write failing tests:** closed issue removed with a tombstone; deleted issue (no closed event) caught by the count check and fixed by one repo full read; transferred issue disappears from the source repo; scheduled reconcile touches only due repos and spreads them across passes.
- [ ] **Step 2-4;** **Step 5: Commit** `feat(github): detect closed, deleted and transferred issues`. (Close PR-G1.)

### Task G4: Organization scoping

**Files:** Modify `herdr_engineering/github.py`, config loader (`herdr_engineering/config.py` — add `github.organizations` with validation: list of non-empty strings, case-insensitive), `config/herdr-engineering.example.yaml`; Test: `tests/test_github_incremental.py`, `tests/test_config.py` (extend)

**Interfaces:**
- Produces: `github.organizations: [login, …]`; empty/absent = all; the snapshot gains `scope: {owners: [...], excluded_repositories: int}`; repos outside scope make zero issue/project calls; on config change the next pass calls `purge_owners_not_in`.

- [ ] **Step 1: Write failing tests:** with allowlist `["acme"]` only acme repos are fetched and stored; excluded count is reported; removing an owner purges its rows and tombstones them; invalid config (non-list, empty strings) is rejected with a clear error.
- [ ] **Step 2-4;** **Step 5: Commit** `feat(github): organization allowlist`.

### Task G5: Rate-limit budget and deferral

**Files:** Modify `herdr_engineering/github.py`; Test: extend `tests/test_github_incremental.py`

**Interfaces:**
- Produces: `github.sync.point_budget` per pass (uses `rateLimit.cost`); when exhausted the pass stops cleanly, records `deferred_repos` (repos not processed, ordered by staleness), keeps the previous cache, and the next pass starts with the most deferred; snapshot exposes `deferred_repos` and sets `stale` when any repo has been deferred past 2x interval.

- [ ] **Step 1: Write failing tests:** budget of N points processes only what fits; deferred repos are first next pass; cache intact; snapshot flags stale/deferred; GitHub `rate_limited` mid-pass behaves the same.
- [ ] **Step 2-4;** **Step 5: Commit** `feat(github): per-pass point budget with visible deferral`.

### Task G6: Issue detail from the store

**Files:** Modify `herdr_engineering/github.py`, `github_store.py`; Test: extend `tests/test_github_incremental.py`, `tests/test_github_store.py`

**Interfaces:**
- Produces: a detail table storing issue body only for issues whose detail was opened, with the `updated_at` it was fetched at; `issue_detail` serves the stored body while the issue's `updatedAt` is unchanged and refetches when the incremental pass saw a change (replacing the 60 s TTL).

- [ ] **Step 1: Write failing tests:** second detail request makes zero calls; after an incremental pass records a newer `updatedAt`, the next detail request refetches once. **Steps 2-4; Step 5: Commit** `feat(github): issue detail cached by updatedAt`.

### Task G7: ETag, revision and `since` deltas on the API

**Files:** Modify `herdr_engineering/control_plane/api.py` (GitHub routes at `api.py:1277-1303`, plus a small ETag wrapper); Test: `tests/test_etag.py`

**Interfaces:**
- Produces: `/api/v1/github/repositories` and `/issues` responses carry `revision` and `ETag: W/"<revision>-<query-hash>"`; `If-None-Match` match returns `304`; `?since=<revision>` returns `{revision, upserted, removed}` or `{resync:true,…full list…}`; a generic helper `etag_json(body)` (content-hash ETag, `304` on match) applied to the read-only endpoints polled by Hosts, Sessions, Services and Missions.

- [ ] **Step 1: Write failing tests:** unchanged GitHub list → second request with `If-None-Match` is `304`; after one change `since` returns only that row; stale `since` returns `resync`; generic endpoint returns `304` for an identical body and `200` after a change.
- [ ] **Step 2-4;** **Step 5: Commit** `feat(api): ETag and since-deltas for GitHub and polled endpoints`. (Close PR-G2.)

### Task G8: Browser consumes ETag/deltas

**Files:** Modify `packages/herdr-web-client/client.js`, `packages/github-view.js`; Test: `packages/herdr-web-client/client.test.js`, extend `tests/playwright/github.test.mjs`

**Interfaces:**
- Produces: client GET helper that stores ETags and sends `If-None-Match`, returning the cached body on `304` without notifying subscribers; GitHub view applies `since` deltas through `patchList` (keyed by `github_id`) and falls back to a full load on `resync`.

- [ ] **Step 1: Write failing tests:** a `304` causes zero view updates; a delta containing one changed issue mutates one row; `resync` replaces the list; the GitHub page shows "owners in scope" and the excluded count. **Steps 2-4; Step 5: Commit** `feat(ui): GitHub page uses conditional requests and deltas`.

### Task G9: GitHub Projects (read-only, org-scoped)

**Files:** Modify `herdr_engineering/github.py` (`ProjectsQuery`, projects phase in `_collect`), `github_store.py` (projects and items tables), `api.py` (`/api/v1/github/projects`), config (`github.projects.*`), Create `packages/github-projects-view.js`; Test: `tests/test_github_incremental.py`, `tests/playwright/github-projects.test.mjs`

**Interfaces:**
- Produces: projects for in-scope owners with `updatedAt` and item count; items fetched only when a project's `updatedAt` or item count changed, keyed by item id with a per-project watermark and the same reconciliation pattern; API supports `ETag`/`since`; the UI shows a board grouped by the project's Status field with a flat-list toggle, rows patched via `patchList`.

- [ ] **Step 1: Write failing tests:** unchanged project → zero item calls; one moved item → one project's fetch; out-of-scope owner's projects never fetched; board groups by Status and the toggle shows a flat list; empty Status column renders as such. **Steps 2-4;** if G0 shows Projects v2 `items` cannot be ordered by update time, fetch all items of a changed project (still skipping unchanged projects) and say so in the code comment and CHANGELOG. **Step 5: Commit** `feat(github): read-only Projects with incremental sync`. (Close PR-G3.)

---

## Collaboration room track (Tasks C0-C7)

Spec section: "Collaboration room editor and shared work". Literal text reaches harnesses unchanged.

### Task C0: Spike — what exists and what pi accepts

**Files:** Create `docs/superpowers/notes/collab-editor-recon.md`

- [ ] **Step 1:** Record where agent names are stored/validated (`collab-mention.js` `NAME`, `collab.py`), how `conversation-composer.js` uploads attachments, which storage collab already uses for them, and whether agents can report skills at registration. **Step 2:** From pi 1.3.0 help/docs record the in-chat syntax for files and skills; the spec's "pass literally" rule stands regardless. **Step 3: Commit** `docs: collab editor recon`.

### Task C1: Addressing model and server-side resolution

**Files:** Modify `herdr_engineering/control_plane/collab.py` (`post_message`, `_MESSAGE_FIELDS`, agent naming), `packages/herdr-web-client/collab.js`; Test: `tests/test_collab_mentions.py`

**Interfaces:**
- Produces: `resolve_mentions(room, text) -> list[{kind, id, name}]` (`kind` person|agent|group); stored `mentions` on the message; alias table old name → `<owner>.<harness>`; name regex `[a-z0-9._-]{1,48}`; `@all/@agents/@here` groups.

- [ ] **Step 1: Write failing tests:** `@gert` resolves person; `@gert.pi` resolves agent; old agent name resolves via alias; second same-harness agent is `gert.pi-2`; unknown `@x` yields no mention; a client-sent `mentions` field is ignored (forged); group expands to room members/agents only. **Steps 2-4:** implement and pass. **Step 5: Commit** `feat(collab): owner.harness addressing and server-resolved mentions`.

### Task C2: Completion engine

**Files:** Modify `packages/collab-mention.js` (sigil providers), `packages/conversation-composer.js` (menu from providers); Test: `packages/herdr-web-components/collab-completion.test.js`

**Interfaces:**
- Produces: `completionAt(text, caret) -> {sigil, query, start} | null`; `providers[sigil](query, ctx) -> Item[]` with `Item {insert, label, section, icon, detail}`; `@` sections People/Agents/Files; `#` issues/PRs; `/` at message start = room commands; after `@agent ` a `/` lists that agent's reported skills. `applyCompletion(text, caret, start, item)` inserts the literal text only.

- [ ] **Step 1: Write failing tests:** `@ge` ranks `gert` before `gert.pi`; `a@b.com` and `C:\path` do not open a menu; `/` mid-sentence does not; `@gert.pi /sk` offers that agent's skills; `applyCompletion` output equals the typed form (no rewriting). **Steps 2-4. Step 5: Commit** `feat(collab): sigil completion engine`.

### Task C3: Chips and composer wiring

**Files:** Modify `packages/collab-view.js` (`mountComposer` call, message render), `packages/session-workspace.js`, `packages/motion.css`; Test: `tests/playwright/collab-editor.test.mjs`

- [ ] **Step 1: Write failing Playwright tests:** menu opens on `@`, arrows/Enter select, Esc closes, focus stays in the textarea; sent message renders person/agent chips with icons; unresolved `@zzz` is flagged before send and sent as plain text; `@gert.pi /skill` text is sent verbatim; the session workspace composer behaves the same. **Steps 2-4. Step 5: Commit** `feat(collab): chips and completion menu in the shared composer`. (Close PR-C1.)

### Task C4: Issue references

**Files:** Modify `herdr_engineering/control_plane/api.py` (`/api/v1/github/resolve?q=`), `collab.py` (`refs` on messages), `packages/collab-mention.js` (`#` provider); Test: `tests/test_collab_refs.py`

**Interfaces:**
- Consumes: G2/G4 store (`search_issues(query, owners)`), in-scope owners only.
- Produces: `#binview-44` and `org/repo#44` resolved to `refs`; shortest unambiguous insert form; chip shows title and state from the cache.

- [ ] **Step 1: Write failing tests:** in-scope issue resolves; excluded owner's issue does not appear in the menu or in `refs`; two owners with the same repo name need `org/repo#n`; a closed issue shows closed; cache empty → menu says "GitHub cache not ready", message still sends. **Steps 2-4. Step 5: Commit** `feat(collab): #issue references from the GitHub cache`.

### Task C5: Room files and shelf

**Files:** Create `herdr_engineering/control_plane/collab_files.py`, `packages/collab-files-view.js`; Modify `collab.py`, `api.py`, `packages/conversation-composer.js` (upload hook), `herdr_engineering/cli.py` (`collab file get|put|list`); Test: `tests/test_collab_files.py`, `tests/playwright/collab-files.test.mjs`

**Interfaces:**
- Produces: `put_file(room, author, name, bytes, parent=None) -> {id, name, version, size, sha256}`; versions immutable; `files` on messages; stable path `.collab/files/<name>`; quotas from `collab.files.max_bytes` (25 MiB) and `collab.files.room_bytes` (500 MiB).

- [ ] **Step 1: Write failing tests:** upload then fetch returns identical bytes and hash; second upload with `parent` creates v2, v1 still readable; oversize and over-quota rejected with a clear error; a non-member cannot read; an agent put creates a version attributed to that agent; shelf lists versions and patches rows without re-render. **Steps 2-4. Step 5: Commit** `feat(collab): room file shelf with immutable versions`. (Close PR-C2.)

### Task C6: Room commands and GitHub write gate

**Files:** Modify `collab.py` (command parser, events), `herdr_engineering/github.py` (`GitHubWriter`, separate token ref), config (`github.write.enabled`, `github.write.token_ref`), `packages/collab-view.js` (cards, confirm dialog); Test: `tests/test_collab_commands.py`

**Interfaces:**
- Produces: events `handoff {from,to,note}`, `decision`, `summary_request`, `issue_draft`, `assignment`; `GitHubWriter.create_issue(owner, repo, title, body)` and `.assign(owner, repo, number, login)`, refused unless `github.write.enabled`.

- [ ] **Step 1: Write failing tests:** `/handoff @a @b note` yields a handoff event addressed to b; `/` not at message start is plain text; `/issue` never calls GitHub until confirmed; with `github.write.enabled: false` it returns a clear "disabled" result; the write token is never the read token and never appears in events or logs. **Steps 2-4. Step 5: Commit** `feat(collab): room commands and gated GitHub writes`.

### Task C7: Handoff status, presence, unread mentions

**Files:** Modify `collab.py`, `packages/collab-view.js`; Test: `tests/test_collab_handoff.py`

- [ ] **Step 1: Write failing tests:** a handoff starts `pending`, recipient accept/decline updates it and posts a card; presence shows "working" while an agent holds an active task; unread mention count per person clears when the room is read. **Steps 2-4. Step 5: Commit** `feat(collab): handoff status, presence and unread mentions`. (Close PR-C3.)

---

## Self-review

- **Spec coverage:** patcher (1), calm/motion (2, 5, 10-12, 16), SSE (3), bus (4), freshness/tokens/dark (5, 13, 17), router and five routes (6-9), per-page polish (10-15), docs (18), view migration (19), G1 store, G2/G3 incremental + reconciliation, G4 org scoping, G5 budget, G6 detail, G7/G8 ETag + deltas, G9 Projects. Operator decisions: org allowlist (G4, G9), dark (5, 17), calm (2, 5, 16).
- **Collab track:** addressing (C1), completion (C2), chips (C3), `#` refs (C4, needs G2+G4), files (C5), commands and write gate (C6), status/presence (C7). The board and `/summarize` content are deferred (spec "later, optional").
- **Gaps accepted:** no webhooks (non-goal). Generic ETag is body-hash (spec ruling).
- **Type consistency:** `patchList/patchText/patchAttr` (Task 1) are the only patch names used later; `tween/flash/setCalm/isCalm/loadCalm` (Task 2) likewise; store method names in G1 are the ones G2-G6 call.
- **Proportion:** tasks give signatures, tests and values; bodies are left to the implementer.
