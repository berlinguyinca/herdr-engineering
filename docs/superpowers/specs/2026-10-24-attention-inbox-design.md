# Attention inbox design

Status: draft for operator review, 2026-10-24. Statements marked **(guess)** were not verified in code or live data.

## Goal

One **Attention** page on the herdr dashboard that answers "what needs me right now?" for the whole fleet, the same on every machine that browses it. Operator requirements carried over:

- The dashboard is browsed from any fabric machine, so acknowledging or snoozing an item on one machine must hold on all of them.
- The operator's own dashboard needs no sign-in. The public collaboration site never serves this page.
- Nothing is lost silently: when a source is unavailable the inbox says so instead of showing "all clear".
- Every item links to the evidence and, where one exists, to the owner screen that can act on it (Fix with Pi, summary fix, CI run, service, host).

Spec `0160-attention-notifications-review.md` is the product intent (coalesced items, lifecycle, policy-based notifications, actions run in their owner systems). This document says how to build it on what exists.

## What exists today

| Piece | Where | What it gives | Limits |
| --- | --- | --- | --- |
| Overview attention panel | `packages/overview-view.js` `overviewAttention` | Items projected in the browser from the polled cards: missions needing attention or failed, failed agents, failed CI, changes-requested reviews, unavailable services, stale hosts, control-plane health, fleet version drift. Sorted by severity, actionability, age. Reports which sources were not loaded (`complete`, `unavailable`). | Computed per browser tab. Snooze is mission-only and lives in `localStorage`, so it does not follow the operator to another machine. No acknowledge, no history, no notification. |
| `AttentionCenter` | `herdr_engineering/attention.py` (spec 0160) | Items with lifecycle `open/acknowledged/resolved/superseded`, dedupe window, quiet hours, severity thresholds, a notifier hook. | In memory only, used by the CLI and the legacy `web.py` page. Not wired into the control plane; nothing raises items into it. |
| Collaboration inbox | `herdr_engineering/control_plane/collab_attention.py` (T15) | Room-scoped items **derived at read time** (review blocked, stale head, CI failed, steering/proposal/consult requests). Only the reader's ack and snooze are stored, as events keyed by `(key, fingerprint)`, bounded per reader and room. Config `control_plane.collab.insights`. | Per room, per reader, for the public site. It is the right model, but it is not the operator's fleet view. |
| Owner-screen actions | `#/ci` Fix with Pi and Send to Pi, session summary fix buttons, session workspace | Idempotent, audited handoffs to an agent. | Reachable only after you have found the run or agent. |

## Gaps

1. No fleet-wide, server-side inbox. Two tabs (or two machines) can disagree about what is open.
2. No acknowledge and no cross-machine snooze for anything except missions, and that one is per browser.
3. No signal for "agent blocked or awaiting input" (spec 0160's first bullet). herdr's `agent_status` idle is unreliable (memory `herdr-agent-status-idle-is-unreliable`), so it must not be the source.
4. New problem sources have no home: LLM problems (`/api/v1/llm/errors`), AI-summary blockers, the CI-fix flow's outcome.
5. No notification, even while the tab is open.

## Proposal

### Derive, do not store

Follow the collaboration inbox: items are **derived at read time** from state the control plane already holds. Nothing is stored per item, so a flood of failing sources cannot grow storage. The only stored things are:

- the operator's **acknowledge** and **snooze** of a `(key, fingerprint)` pair, written as control-plane events (`attention_ack`, `attention_snooze`, `attention_clear`) so they replay and are shared by every browser;
- nothing else. "Resolved" is shown from a bounded in-memory ring (below), not stored durably.

`key` names the thing (`ci:17:12`, `session:<id>`, `host:beast`); `fingerprint` is a short hash of what is wrong with it (status, failing step, error class). When the condition changes, the fingerprint changes and an acknowledged or snoozed item **reopens**, so a snooze can never hide a different problem.

### Item

```yaml
key: str                 # stable identity of the subject
fingerprint: str         # changes when the condition changes
kind: str                # category, table below
severity: critical|warning|info
title: str               # one line, already redacted
detail: str              # one line, bounded
href: str                # hash link to the evidence, same host
actions: [ {id, label, href} ]   # deep links to the owner screen; see "Actions"
source: str              # which source produced it
first_seen / last_seen: iso time   # first_seen survives while the key stays continuously open
count: int               # occurrences coalesced into this item (for example 9 failed runs of one repo on one branch)
state: open|acknowledged|snoozed
snoozed_until: iso time|null
```

### Sources

Existing projections move server-side unchanged in meaning; the new rows are marked **new**.

| Kind | Trigger | Severity | Evidence link | Owner action |
| --- | --- | --- | --- | --- |
| `mission_attention`, `mission_failed` | mission health needs attention / failed | warning / critical | `#/missions/<id>` | open mission |
| `agent_failed` | session outcome failed | critical | `#/herd/session/<id>` | open agent |
| `agent_blocked` **new** | the session's latest AI summary has non-empty `blockers` and the agent is active | warning | `#/herd/session/<id>` | summary fix buttons (already exist) |
| `ci_failed` | failed run, coalesced per repository and branch | critical | `#/ci?tab=failed&...` | Fix with Pi / Send to Pi |
| `review_changes` | review changes requested | warning | `#/reviews?...` | open review |
| `service_down` | herdr-scope service unavailable | critical | `#/services/<id>` | open service |
| `host_stale` | no recent healthy report | warning | `#/hosts/<id>` | open host |
| `control_plane` | health not ok | critical | `#/topology` | none |
| `fleet_drift` | tool or plugin versions differ | warning | `#/hosts?drift=1` | none |
| `llm_problems` **new** | one error class with count >= threshold in the window, from the LLM stats | warning | `#/llm?...` | open LLM page |
| `source_unavailable` | a source could not be read | warning | the source's page | none |

`agent_blocked` deliberately reads the structured summary the control plane already stores, not the terminal screen. Detecting a blocked agent from its screen is left as an open question (below); it would be a new, noisier signal.

`source_unavailable` is how "unknown" is shown: when a source cannot be read, the inbox lists that fact as an item (and the page header says "Partial") instead of silently dropping its items.

### Lifecycle

- **open**: derived and not acknowledged or snoozed (or its fingerprint changed since).
- **acknowledged**: the operator has seen it; it stays listed, dimmed, until the condition clears or the fingerprint changes. Acknowledge does not hide it from the count of "still true" problems.
- **snoozed**: hidden from the open list until `snoozed_until` (bounds in YAML, default 1 h to 7 d) or until the fingerprint changes.
- **resolved**: the condition is no longer derived. The last 100 resolved items from the past 24 h are kept in memory and shown under "Recently resolved". After a control-plane restart that list starts empty **(decision, see below)**.
- `superseded` from spec 0160 is not used: a changed fingerprint reopens the same key instead of creating a second item.

### Actions

The inbox performs no action of its own. Per spec 0160, actions run in their owner systems with their own authorization; the inbox only deep-links to them (for a failed CI run, to the run with Fix with Pi ready; for an agent blocker, to the session's summary with its fix buttons). One-click actions from the inbox are a later step and would call the same existing endpoints, never a second path.

### Notifications

First version, **browser only**, matching spec 0160's "local/browser first":

- Only while a dashboard tab is open and the operator granted the browser permission. Critical items only by default.
- One notification per `(key, fingerprint)`; a reopened item with a new fingerprint notifies again. Quiet hours suppress them.
- The payload is the item `title` and a link back to `#/attention`. No log text, no secrets, no detail line.
- Because the inbox state is server-side, two open tabs notify once each at most and an acknowledgement on one machine stops the other from notifying about the same fingerprint on its next poll.

External channels (push, chat) are adapters for a later design.

## API

All routes are on the **operator listener only**; none is added to `_PUBLIC_STATIC` or the collab allowlist.

```
GET  /api/v1/attention?state=open|all&kind=&severity=
       -> {items:[...], counts:{open,acknowledged,snoozed,resolved}, partial, unavailable:[source...], generated_at}
POST /api/v1/attention/ack      {key, fingerprint}
POST /api/v1/attention/snooze   {key, fingerprint, seconds}
POST /api/v1/attention/clear    {key, fingerprint}        # undo an ack or snooze
```

POSTs pass the existing same-origin guard and operator role. `key` and `fingerprint` are length-capped and charset-checked, and an ack for a pair the server does not currently derive is accepted but stored only if it matches a currently open item (so the event log cannot be filled with arbitrary pairs). A per-minute cap on acks applies.

## Page

`#/attention`, a new top-level nav entry with a badge showing the open count (critical in a stronger tone). The Overview "Attention" panel reads the same endpoint and keeps today's presentation, so the two never disagree. If the endpoint is unreachable the Overview falls back to today's browser projection and says so.

```
Attention                       12 open · 3 acknowledged · 2 snoozed     [Partial: CI unavailable]
critical  CI failed · org/app main · 3 runs            2m   [Open run] [Acknowledge] [Snooze ▾]
critical  Service down · metabolomics-web              9m   [Open service] [Acknowledge] [Snooze ▾]
warning   Agent blocked · Amber Swift                  14m  [Open agent] [Acknowledge] [Snooze ▾]
warning   LLM problems · upstream_admission ×41 (24h)  1h   [Open LLM] ...
▸ Acknowledged (3)   ▸ Snoozed (2)   ▸ Recently resolved (6)
```

It works at phone width, and every state (loading, empty, partial, error) is explicit. "No items" is shown only when every source loaded.

## Configuration (YAML only)

```yaml
sections:
  control_plane:
    values:
      attention:
        enabled: true
        snooze: {min_seconds: 3600, max_seconds: 604800}
        thresholds: {host_stale_seconds: 600, llm_error_count: 20, llm_window: 24h}
        notify: {browser: true, min_severity: critical, quiet_hours: {start: 22, end: 7}}
        resolved: {keep_hours: 24, max_items: 100}
```

Nothing reads environment variables. With `enabled: false` the routes answer 404 and the Overview keeps its browser projection.

## Security and privacy

- Items are derived from records the dashboard already shows; titles and details go through the existing redaction and length bounds. No log text enters an item.
- Operator listener only. A regression test asserts every `/api/v1/attention*` route, `#/attention` and the new JS module return 404 on the public port.
- Acks and snoozes are audited events carrying the actor the control plane already records.
- Bounded everything: items (200 total, 50 per kind), stored ack events, resolved ring, request rate.
- Notification payloads carry a title and a link only.

## Test plan

- **Derivation (`tests/control_plane/test_attention.py`):** each source produces its item with the right severity and link; coalescing (nine failed runs of one repo and branch give one item with `count` 9); an unavailable source yields `source_unavailable` and `partial`, never a clean list.
- **Lifecycle:** ack keeps the item listed and does not hide it; snooze hides it; a changed fingerprint reopens both; snooze bounds are enforced; expired snooze reopens; resolved ring bounds and age.
- **Storage:** ack events replay after a restart (use the sink test pattern from `test_token_usage_retention.py`); an ack for a pair that is not currently open is not stored; caps hold under a flood.
- **API:** routes and filters, 400 on bad input, operator-only, public-listener 404, `enabled: false` gives 404.
- **UI (node unit + Playwright):** states render (open, partial, empty, error); acknowledge and snooze from one browser context show in a second context; badge count; 390 px width without horizontal scroll; Overview and Attention agree; fallback when the endpoint fails.
- **Notifications:** one per fingerprint, none during quiet hours, payload has no detail text.

## Build plan (PR-sized)

1. **Derivation and state.** `control_plane/attention_inbox.py` (pure derivation over the repo, reusing the existing projection rules), the ack/snooze events and fold, `GET /api/v1/attention` and the three POSTs, config, public-404 test.
2. **Page and nav.** `#/attention`, badge, Overview panel switched to the endpoint with the browser fallback. Playwright cross-context ack test.
3. **Notifications.** Browser notifications with dedupe and quiet hours.
4. **New sources.** `agent_blocked` from summary blockers, `llm_problems`, and the CI-fix outcome. Each is one derivation function plus tests.
5. **(Later, separate design)** one-click actions from the inbox, external notification adapters, screen-based "blocked" detection.

## Decisions needed from the operator

Defaults below stand until you answer; each can change on request.

1. **Replace or sit beside the Overview panel.** Default: the Overview reads the same endpoint, so there is one source of truth.
2. **Resolved history after a restart.** Default: in memory only, so it is empty after a restart. Alternative: store a resolved event per item, which adds storage that grows with problem churn.
3. **Notifications.** Default: browser only, critical only. Say if you want warnings too, or want an external channel designed now.
4. **Blocked agents.** Default: only from the AI summary's `blockers`. Alternative: classify from the pane screen, which catches "waiting for input" sooner but is noisy and was shown unreliable for status.
5. **Scope of acknowledge.** Default: one operator view for everyone who can open the dashboard (it has no per-person identity). Alternative: per-browser, which brings back the cross-machine problem.
6. **Collaboration inbox.** Default: stays separate (room-scoped, reader-scoped, public site). The dashboard inbox may later show a single "N collaboration items" link, but never their contents.

## Decisions applied (defaults, 2026-10-24)

All six decisions above were built at their stated defaults: the Overview reads the same endpoint; resolved history is in memory only; notifications are browser-only and critical-only; blocked agents come only from AI-summary `blockers`; acknowledge and snooze are one shared operator view; the collaboration inbox stays separate.

### Slice 1 notes (derivation, state and API, as built)

- `control_plane/attention_inbox.py` holds the derivation, the config validator and `AttentionInbox`. The browser's `statusOutcome` and `missionHealth` are ported to Python (`status_outcome`, `mission_health`) so server and browser classify alike.
- Stored state is one private `attention`/`operator` event stream (`attention_ack`, `attention_snooze`, `attention_clear`), recorded with `feed=False, publish=False`, so it replays through `export_state`/`import_state` and the durable sink but never reaches `/activity` or the SSE feed. Each event carries the actor the control plane records (`operator:<key digest>`).
- A write is stored only when `(key, fingerprint)` matches a currently derived item; otherwise the answer is `{stored: false, reason}`. A repeated identical ack is not stored again. Writes are capped at 30 a minute (429). Snooze seconds outside `snooze.min_seconds..max_seconds` are a 400, not clamped.
- Fingerprints are a hash of the kind plus the condition (`failed`, `stalled`, `down` vs `stale` ...), not of timestamps, so a new failing run on the same branch does not reopen a snooze but a different kind of problem on the same key does.
- **Deviation: CI.** A failed run is coalesced per repository and branch as the count of consecutive failures from the newest run backwards; a newer green run clears the item (the browser projection kept every failed run). Runs still running are skipped over.
- **Deviation: failed windows.** Failed missions and agents older than `thresholds.failed_window_hours` (new key, default 168) are not items, so old failures do not live in the inbox for ever. The browser projection had no such bound.
- **Deviation: missions.** `mission_attention` needs a live report (freshness `live`) and a current lifecycle, exactly as the catalog groups them; a stale or disconnected report is never an incident.
- A source that raises, or CI that is stale, `partial`, `unavailable`, `auth_error` or still syncing with no data, becomes a `source_unavailable` item and `partial: true`. `not_configured` CI is not an outage. Items of an unavailable source are never moved to Resolved.
- `ControlPlaneServer(attention=...)` takes the validated config; `cli.py` validates `control_plane.attention` and exits on a bad key. `enabled: false` answers 404 on all four routes. The POST routes pass the same-origin and JSON guard used for reviews and CI.
- Not built in this slice: the page, nav badge and Overview switch (slice 2), notifications (slice 3), `agent_blocked` and `llm_problems` (slice 4).

### Slice 2 notes (page, badge and Overview, as built)

- `packages/attention-view.js` renders `#/attention` (pure `renderAttention`, `mountAttention`) from `GET /api/v1/attention?state=all`, polling every 5 s while visible. Acknowledged, snoozed and recently resolved items are collapsed groups; the open list shows the item's evidence link, its owner link and Acknowledge / Snooze (1 h, 4 h, 24 h, 7 d) controls. Acknowledged and snoozed items offer Reopen / Wake now (the `clear` route). Severity and kind filters are client-side over the answer.
- States are explicit: loading, a hard error (`herdr-error`, retry automatic), a stale list with an error banner, partial (names the unavailable sources and never claims "no items"), empty (only when every source was read), and "turned off" when the route answers 404.
- Item ages show `first_seen` (how long the condition has been continuously open), not the source timestamp, because several sources (control plane, fleet drift) have none.
- The nav badge (`herdr-app-shell.setBadge`) is fed by a separate 15 s poll on every page, shows the open count, and turns critical-toned when any open item is critical. An unreachable inbox leaves the badge as it was.
- The Overview "Attention" panel reads the same endpoint (`serverAttention`). Before the first answer it shows "Loading attention…"; if the endpoint fails it falls back to the browser projection with a visible "computed in this browser and may differ" warning; if the route is 404 (feature disabled) the projection stays with no warning. The Overview's per-browser mission snooze (the missions card) is unchanged.
- Test fixture: `tests/playwright/server.py` gains `ATTENTION_SEED=1` (one failed agent) and `CI_OFFLINE=1` (the fixture host may hold a real Woodpecker token; a hermetic page must not list real runs).
- Three existing Overview Playwright tests (`ui.test.mjs`) drive the browser projection with mocked sources; they now answer `/api/v1/attention` with 404 (the "turned off" path, which keeps the projection) and start the fixture with `CI_OFFLINE=1`, because this host's real Woodpecker token otherwise floods the list with real runs.

### Slice 3 notes (browser notifications, as built)

- The policy is YAML, not browser code: `GET /api/v1/attention` now carries `notify` (`browser`, `min_severity`, `quiet_hours`) from `control_plane.attention.notify`, and the browser only applies it. `notify.browser: false` hides the control and stops announcing.
- `packages/attention-notify.js` holds a pure planner (`planNotifications`, `quietNow`) and a small notifier. The operator opts in per browser with an "Enable notifications" button on `#/attention` (the Notification API needs a click); the choice is kept in `localStorage`.
- One notification per `(key, fingerprint)`, remembered in `localStorage` (bounded to 500 ids) and re-read before announcing, so two tabs of one browser announce once. A changed fingerprint announces again; an acknowledge or snooze from any machine removes the item from the open list, so the next poll announces nothing for it.
- **Baseline rule (not in the design):** items already open when notifications are switched on, or when a tab starts, are remembered without notifying, so opening the dashboard never produces a burst. Only items that appear while a tab is open announce.
- Quiet hours use the browser's local clock and wrap midnight. Items held back by quiet hours are **not** marked seen, so a critical item still open when the hours end is announced then.
- More than three new items in one poll collapse into one notification ("N new attention items") with no item text. Every notification has an empty body, the item title as its title, and a click that opens `#/attention`; no detail or log text is ever in the payload.
- A hidden tab keeps its 15 s inbox poll only while notifications are on and permitted; otherwise hidden tabs stay quiet like every other poll.
- Test fixture: `ATTENTION_TRIGGER=<file>` makes `tests/playwright/server.py` add a second failed agent when the file appears, so the notification test can create a new item after the baseline.

### Slice 4 notes (new sources, as built)

- **`agent_blocked`** reads the structured AI summary the control plane already stores (`agent_summary_events`), never the pane screen. An item exists for an agent that is still active (the same `status_outcome` rule as everywhere), whose latest summary has at least one non-empty `blockers` entry and was generated within `thresholds.blocked_window_hours` (new key, default 24). Stale summaries are ignored so an old blocker cannot outlive the agent's real state. A failed agent is `agent_failed`, not also blocked. The fingerprint hashes the blocker texts, so a snooze holds for the same blocker and a different blocker reopens it. Key `blocked:<session id>`, source `summaries`.
- **`llm_problems`** folds `repo.llm_errors(thresholds.llm_window)` (proxy records only; a transcript has no status) per error class across models and raises one warning item per class whose count reaches `thresholds.llm_error_count`. The key is `llm:<class>` and the fingerprint is the class alone, so an acknowledge holds while the count grows and the count shows in the item. It links to `#/llm?window=<window>` (the LLM page has no error-class filter). Source `llm`.
- Both sources are listed in `source_unavailable` handling like every other source: if the summaries or the LLM stats cannot be read the inbox says so and does not resolve their items.
- **Not built:** the CI-fix outcome source named in the build plan. The CI fix agent is being built in a parallel stream (its request and state machine landed on main in #227); an inbox item for its outcome is one more derivation function to add once that state is readable from the repo, and is left to that stream.
- Closing summary of deviations from the design: CI coalescing counts consecutive failures per repository and branch and clears on a newer green run; failed missions and agents older than `failed_window_hours` (default 168) are not items; `mission_attention` requires a live report; the baseline rule and the "held, not dropped" quiet-hours rule for notifications; `blocked_window_hours` as a new threshold.
