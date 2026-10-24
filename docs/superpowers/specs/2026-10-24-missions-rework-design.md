# Missions page rework design

Status: draft for operator review, 2026-10-24. Statements marked **(guess)** were not verified in code or live data.

## Goal

The operator's words: the missions page is "kinda pointless and doesn't really tell us what was going on, it's missing data". The rework makes a mission page that answers, for a mission that is running **and for one that finished a week ago**:

1. What was it meant to do, on which repository and branch, on which host?
2. What happened, in order, in words (not just stage names)?
3. How did it end: did the tests pass, did CI pass, was a pull request opened or merged, what did it cost?
4. What needs me now (blocked, waiting, failed)?
5. Where do I go next (the agents, the pull request, the CI run, the LLM usage)?

Constraints carried over: private operator dashboard only, remote text untrusted, existing routes keep working, YAML-only config, honest "unknown" instead of zeros, and the live/recorded source namespaces stay exact (a `pi:` key is never merged with a herdr id by guess).

## What exists today

- **Recorded missions** (`repo.create_mission` / `upsert_mission`): `title`, `purpose`, `stage`/`status`, `cwd`, `source`, `repository_id`, `commit_sha`, `created_at`, `updated_at`, and the list of sessions. A mission stream holds `MissionCreated/Started` and stage changes.
- **Timeline** (`repo.mission_timeline`): stage changes, test runs, CI runs, reviews and pull requests, as compact rows with no payloads.
- **Live read model** (`control_plane/mission_activity.py`, Pi missions): current objective, current activity, wait reason and duration, workers, weighted progress, freshness. It is **ephemeral and bounded** (128 missions, 24 h retention) and by design never publishes lifecycle events.
- **Pages:** the catalog (`mission-catalog-view.js`, from the 2026-10-20 focused-missions work: compact rows, current/attention/last-known/recorded groups, filters) and the detail (`viewMission` in `app.js`: "Recorded history" with brief, recorded agents, timeline, raw event stream; plus the live panel for `pi:` missions).
- **Per-session AI summaries** (`agent_summaries.py`): what each agent did, blockers, next steps.
- **Usage records** carry `mission_id` (the LLM page can filter by `mission=`).

## Why it feels empty (findings)

1. **The story evaporates.** Everything that explains a Pi mission (objective, activity, waits, workers, progress) lives in the live read model, which expires after 24 h. A mission from last week shows only a title, a stage and a thin timeline. Durable state is owned by Pi and is not copied into herdr **(guess: confirmed by the module docstring and the 24 h constant, not by inspecting live data)**.
2. **No outcome.** Nothing on the page says whether the mission succeeded: the linked tests, CI runs, reviews and pull requests exist in the repo but are shown as timeline rows, not as a result.
3. **No cost or effort.** Duration, agent count and token usage are not shown, although the data (session times, usage records with `mission_id`) exists.
4. **Agents are a list of names.** The page does not say what each agent did; the AI summaries exist per session but are not used here.
5. **Dead ends.** The page links to related reviews and CI by filter, but not to the pull request, the host, or the LLM usage (see the cross-linking design).
6. **The list rows describe the present only.** A finished mission's row has no outcome and no "last meaningful event".

## Proposal

### 1. A durable mission outcome record

Make the story survive the live window. When a mission **ends** (any terminal stage), and at most once per terminal transition, the control plane writes one bounded **`MissionOutcome`** event on the mission stream. It is assembled from data the control plane already has at that moment:

```yaml
schema: 1
ended_at: iso time
duration_s: int|null            # first start to end; null if either is unknown
final_stage: str
objective: str                  # from the live projection if present, else purpose; bounded, redacted
last_activity: str|null         # last known current-activity line; bounded, redacted
agents: [{session_id, label, role, model, started_at, ended_at, outcome, summary}]   # up to 12; summary = the AI summary headline, bounded
tests: {runs, passed, failed}                      # counts only
ci:    [{repository, branch, run, state}]          # up to 5, latest per repo and branch
reviews: {open, approved, changes_requested}
prs:   [{repository, number, state, url}]          # up to 5
usage: {requests, input_tokens, output_tokens, lower_bound}   # from usage records with this mission_id
sources: [str]                  # which sources contributed; a missing source is named, never zero
```

It is an event like any other (durable, replayed, retention follows the existing event retention), not a new table. Fields a source could not supply are `null`, and `sources` lists what was read, so an outcome assembled while CI was unreachable says so. Missions that ended **before** this ships get no outcome; the page says "recorded before outcomes were kept" rather than inventing one.

Edge cases decided up front:

- A mission that never reports a terminal stage (Pi crashed) gets a **stale-close**: after `stale_close_hours` (default 72) without any event, a `MissionOutcome` with `final_stage: unknown` and `sources` marking it as inferred. It is never reported as success.
- A mission that restarts after ending reopens: the old outcome stays in the stream, and the page shows the latest.
- The write is idempotent per `(mission, terminal transition)`.

### 2. Mission page, top to bottom

```
‹ Missions    Fix flaky parser test                       [Succeeded]  2h 14m  ·  3 agents  ·  1.2 M output tokens
org/metabolomics-ui · branch fix/parser · host beast       PR #214 merged · CI passed · tests 128 passed

Needs you now      (only when something is open)  Waiting: approval of the plan, 14 min   [Open agent]
What happened      ▸ 11:02 Started on beast (Amber Swift, pi, qwen3.8-27b)
                   ▸ 11:20 Tests failed (3)  ·  11:41 Tests passed (128)
                   ▸ 12:05 PR #214 opened  ·  12:14 CI passed  ·  12:30 PR merged
                   ▸ 13:16 Ended: complete
Agents             Amber Swift  pi  1h 50m  "Fixed the flaky timing assertion…"            [Open agent] [LLM usage]
Outcome & cost     Tests · CI · Reviews · Pull requests · LLM (requests, tokens, problems)
Related            hosts, repository, reviews, CI (cross-linking panel)
▸ Brief and scope   ▸ Raw event stream (kept)
```

- **Header:** status chip, duration, agent count, tokens, the repository/branch/host line, and a one-line outcome (PR, CI, tests). Values that are unknown show "—" or "not reported", never 0.
- **Needs you now:** only for open conditions, using the same derivation as the attention inbox so the two never disagree (see the attention inbox design).
- **What happened:** a merged chronological narrative of meaningful events only (stage changes, agent start and end, tests, CI, reviews, pull requests, handoffs, waits longer than a threshold), each one line with its link. Routine noise (heartbeats, telemetry) is excluded; the raw stream stays one click away. Waits are shown as a span ("waited 14 min for approval"), not as a point.
- **Agents:** one row per agent with role, model, time span, outcome and the AI-summary headline, linking to the agent and its LLM usage.
- **Outcome & cost:** the result of each linked thing with a link, plus LLM usage from usage records filtered by `mission` (the LLM page stats API already supports it).
- **Running missions** use the same layout; the live read model fills the top (objective, current activity, wait reason, progress) and the narrative is live. Disconnected or stale telemetry is labelled as today and never turned into an incident (the 2026-10-20 behaviour contract still holds).

### 3. Catalog rows that say what happened

Each row gains, for missions with an outcome: a result chip (succeeded, failed, ended unknown), duration, agents, and a one-line "last meaningful event" (for example "PR #214 merged"). Filters add result (succeeded / failed / unknown) and repository. Sorting stays newest-first with current work and attention first. Today's search, host and status filters, polling and focus-preserving behaviour are unchanged.

### 4. A mission narrative API

`GET /api/v1/missions/<key>/story` on the operator listener returns the page's data in one answer: `{header, needs, events, agents, outcome, usage, related, partial}`. It reads the outcome event when the mission has ended, or assembles the same shape live while it runs, so the page does not stitch five requests together and the assembly logic is tested in one place. Bounded (events capped at 200 with `more`), redacted, and `partial` names any source that could not be read. The existing mission, timeline and stream routes are unchanged.

## Security and privacy

- Operator listener only; the story route, the outcome-writing code and the new JS return 404 on the public port, asserted by a test.
- The outcome stores summaries, counts and ids, never message or log bodies. AI summaries are already redacted and bounded; they are re-bounded here and treated as untrusted text (escaped, linkified only by the safe linkifier).
- Agent labels, objectives and CI step names are untrusted strings: escaped, length-capped, never used as `href` or HTML.
- Everything is bounded (agents, CI, PRs, events, bytes per outcome) so a noisy mission cannot grow the event log.

## Configuration (YAML only)

```yaml
sections:
  control_plane:
    values:
      missions:
        outcomes: {enabled: true, stale_close_hours: 72, max_agents: 12}
        story: {max_events: 200, wait_span_seconds: 300}
```

With `outcomes.enabled: false` no outcome events are written and pages show what exists today. Nothing reads environment variables.

## Test plan

- **Outcome assembly (python):** a seeded mission with sessions, test runs, CI, a review, a PR and usage records yields the expected outcome; a missing source gives `null` and is named in `sources`; counts are never invented; bounds hold; the outcome is idempotent per terminal transition; a reopened mission shows the latest; stale-close never reports success; the event replays after a restart.
- **Story route:** each section from seeded data; `partial` when a source fails; live assembly for a running mission equals the outcome shape; 404 for an unknown key; operator-only; public-listener 404.
- **Namespaces:** a `pi:` key never matches a herdr mission with the same raw id (existing guarantee, kept under test).
- **UI (node unit plus Playwright):** the layout renders for succeeded, failed, running, stale and pre-outcome missions; unknown values show placeholders; every dynamic string is escaped; links reach the agent, PR, CI run and LLM usage; catalog result chips and filters; 390 px width without horizontal scroll; the live panel still shows stale and disconnected states correctly.
- **Existing mission tests** (stale/disconnected/updating, route races, zero mutation) stay green.

## Build plan (PR-sized)

1. **Outcome event.** Assembly from the repo, terminal-transition hook, stale-close sweep, config, tests.
2. **Story API.** The route, live assembly, caps, public-404 test.
3. **Mission page.** The layout above on the story API, keeping the raw stream and the live panel; the cross-linking Related panel when that lands.
4. **Catalog.** Result chips, last meaningful event, result and repository filters.
5. **(Later)** backfill outcomes for old missions from their timelines where enough is recorded, clearly marked as reconstructed.

## Decisions needed from the operator

Defaults stand until you answer; each can change on request.

1. **Outcome durability.** Default: one `MissionOutcome` event per ended mission, kept under the normal event retention. Alternative: keep outcomes longer than other events (a separate, longer retention), which preserves history but needs its own retention rule.
2. **Old missions.** Default: no backfill; they show "recorded before outcomes were kept". Alternative: reconstruct from timelines in step 5, marked as reconstructed.
3. **Stale-close.** Default: 72 h of silence closes a mission as `unknown`. Say if a mission can legitimately stay quiet longer.
4. **What counts as success.** Default: the final stage (`complete`) decides the chip; tests, CI and PR state are shown next to it and never override it. Alternative: derive "succeeded" only when CI and tests also passed, which is stricter but can contradict the owning system.
5. **Pi-owned state.** Default: herdr copies a bounded outcome at the end. Alternative: ask Pi for durable history on demand, which keeps one owner but needs a Pi API **(guess: none exists today)**.
