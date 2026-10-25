# Review Gate Dashboard Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Review gate" page in the control-plane dashboard that shows every gated PR, the review queue, main-branch status and recent notices, and lets an operator resume an escalated PR.

**Architecture:** One new view module `packages/review-gate-view.js` (pure render functions + `mountReviewGate(container, client)`), registered as route `/review-gate` in `packages/app.js` and in `NAV`. It polls four existing read routes every 5 s and renders from the last good snapshots. No server changes except none; the routes exist.

**Tech Stack:** Plain ES modules, `node --test` (`packages/herdr-web-components/*.test.js` style), no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md` (dashboard page, escalation clear: "a dashboard button"). Earlier plans in `docs/superpowers/plans/`.

## Data sources (all exist)

- `GET /api/v1/review-gate/prs` -> `[{key, head_sha, state, review_rounds, ci_fix_rounds, reason, approved_sha, last_findings:[{path,line,severity,message,required_change}]}]`. `key` is `github.com/<owner>/<name>#<number>`.
- `GET /api/v1/review-gate/queue` -> `{depth, jobs:[{job_id,key,sha,author_model,enqueued_at,state,lease_expires_at}], gate:{last_tick_at,counts,last_error}}`.
- `GET /api/v1/review-gate/notices` -> `[{key,sha,kind,channel,ok,at}]` newest first.
- `GET /api/v1/review-gate/main` -> `{repos:[{repo,branch,head_sha,state,last_green_sha,failures,range_total,culprits}]}`.
- `POST /api/v1/review-gate/prs/<canonical repo path>/<number>/resume` (operator only; body `{}`), e.g. `/api/v1/review-gate/prs/github.com/acme/app/12/resume`. 401/403 = no operator key; 404 = gate disabled.
- A 404 on any read means the gate is disabled: show one "Review gate is not enabled on this control plane" note and stop polling.

## Global Constraints

- Every string from the API is untrusted (PR findings and commit-derived text especially): escape with `escapeHtml` from `./herdr-web-components/util.js`; never `innerHTML` raw API text; shas shown as the first 12 chars; no links built from API text.
- A failed or slow read keeps the last good data and shows a visible "showing data from <time>" warning for that panel; it never blanks the page.
- Polling stops on `dispose()`, aborts in-flight requests, and does not stack requests (skip a tick while one is in flight).
- The resume button asks for confirmation, only appears for `escalated` PRs, disables while in flight, and shows the server's refusal (403/404/other) verbatim-safe (escaped) instead of failing silently.
- Accessible: table headers, `role="status"` for warnings, buttons reachable by keyboard; works at phone width (stacked cards under 700px).
- Match the surrounding view style (compact modules, `hd-btn`, design-system CSS variables); no inline colour literals outside the page's `<style>`.

## Review Focus

- Hostile finding text / PR keys with markup or huge length: no markup executes, lines are clipped.
- Gate disabled (404 everywhere), partial failures (one route fails), empty data (no PRs, empty queue, no main repos).
- Resume by an unauthorised viewer: the button's failure is shown, state does not change locally.
- Polling lifecycle: navigate away and back repeatedly; no duplicate timers.

---

### Task 1: View module with pure renderers

**Files:**
- Create: `packages/review-gate-view.js`
- Test: `packages/herdr-web-components/review-gate.test.js`

**Interfaces:**
- Produces (all exported, pure, return HTML strings): `renderGateSummary(queue)`, `renderMainStatus(main)`, `renderPrTable(prs, jobsByKey)`, `renderPrDetail(pr)`, `renderQueue(queue)`, `renderNotices(notices)`; helpers `shortSha(sha)`, `resumePath(key)` (key `github.com/o/n#12` -> `/api/v1/review-gate/prs/github.com/o/n/12/resume`, returns `null` for anything not matching `^github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$`), `stateLabel(state)` mapping the gate states (ci_pending, ci_green, review_queued, reviewing, changes_requested, approved, merged, ci_red, superseded, escalated) to plain words and an unknown state to a safe literal.

- [ ] **Step 1: Tests**: `shortSha clips to 12 and tolerates junk`, `resumePath builds the route and refuses hostile keys` (keys with `../`, spaces, `<`, missing number), `stateLabel covers every state and escapes unknown`, `renderPrTable escapes hostile key/reason and shows rounds`, `renderPrDetail lists findings escaped and clipped`, `renderPrDetail shows resume only for escalated`, `renderMainStatus shows state, last green, failures and culprit PR numbers`, `renderMainStatus tolerates missing fields`, `renderQueue shows depth, claimed vs queued, gate last error`, `renderNotices shows channel and ok flag`, `all renderers accept null/undefined/empty`.
- [ ] **Step 2: Run, expect failures.** `node --test packages/herdr-web-components/review-gate.test.js`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Commit** `feat(dashboard): review gate renderers`.

### Task 2: mount, polling, resume, route

**Files:**
- Modify: `packages/review-gate-view.js` (add `mountReviewGate`)
- Modify: `packages/app.js` (import, `NAV` entry `{ path: '/review-gate', label: 'Review gate' }` after Reviews, `ROUTES['/review-gate']`, view function setting `dashboardView = mountReviewGate(root, client)`)
- Test: `packages/herdr-web-components/review-gate.test.js` (mount tests with a fake `client.request` and a minimal fake `container`/timers, in the style of the other mount tests; if none exist for another view, inject `setInterval`/`clearInterval`/`fetch` via an options argument `{timers}`)

**Interfaces:**
- Produces: `mountReviewGate(container, client, {intervalMs = 5000, timers = {setInterval, clearInterval}} = {}) -> {dispose()}`. Reads via `client.request(path, {signal})`. Selecting a PR row shows its detail panel (state kept across polls by key). Resume: `client.request(resumePath(key), {method:'POST', body:{}})` after `window.confirm`; then an immediate refresh.

- [ ] **Step 1: Tests**: `mount polls the four routes and renders`, `mount keeps last good data and warns when a read fails`, `404 from every route shows the disabled note and stops polling`, `dispose stops polling and aborts in-flight reads`, `no overlapping polls`, `resume posts to the route after confirm and refreshes`, `resume shows the refusal on 403 and leaves state unchanged`, `resume not offered for non-escalated PRs`, `selection survives a poll`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the client tests and Python route/static tests.** `npm run test:client` and `PYTHONPATH=. pytest tests/control_plane/test_web.py -q`; if a test pins the NAV list or static file list, update it.
- [ ] **Step 5: Commit** `feat(dashboard): review gate page`.

### Task 3: Docs

**Files:**
- Modify: `docs/review-gate.md` (new short section "Dashboard": what the page shows, the resume button and its operator-key requirement, the 5 s refresh, what a warning means)

- [ ] **Step 1: Write the section; cross-check every route and field name against the Data sources list above.**
- [ ] **Step 2: Commit** `docs(review-gate): dashboard page`.
