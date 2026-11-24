# /summary snapshot history and whole-session coverage — design

Date 2026-11-21. Status: implemented in the same PR. Builds on the drift graph (`2026-11-04-session-drift-graph-design.md`) and segment metrics (`2026-11-04-segment-metrics-design.md`).

## Request

"Update the summary feature to have the whole history in it and not just the snapshot, and have a history plot of the snapshots on the bottom."

## Prior plans (searched, none for this)

Searched `docs/`, `git log --all --grep=summary|snapshot`, all local and remote branches and every PR (`gh pr list --state all --search summary`, PRs #330 to #660 and the open ones). No spec, plan, branch or PR covers summary history or a snapshot series. The only related text is a limitation in `docs/operations/session-summary.md` ("Long sessions are elided in the middle", "A report is a snapshot") and the drift-graph spec's remark that the 400-event trim drops "exactly where drift happens", which it fixed for prompts only. So this design is new; it keeps the drift-graph rules: numbers come from code, failure is soft, the viewers are pure draw loops over sidecars.

## Goals

1. A report reflects the **whole** session, not the first 60 plus last events.
2. Every `herdr-eng summary publish` **keeps** the earlier snapshots instead of overwriting them.
3. A **history plot** of the snapshots sits at the bottom of the summary view in the terminal, and under the report in the dashboard summary card.
4. Every plotted number is computed by code from the session history. The model writes none.

## Non-goals

- Re-running old reports; snapshots exist only from this release on.
- Plotting test pass/fail. No harness marks a command as a test run, so it would be a text heuristic, which the drift-graph spec forbids for numbers. Failed tool calls are plotted instead (the harnesses record `ok` for them).
- Snapshots for dashboard-triggered runs (`summary report`, host worker). They run in a scratch process with no terminal cache; deferred.

## Whole history within model context

The cap on events stays: dropping it would blow the evidence limit (100,000 characters) on any long session. Instead the elided middle is **replaced by a code-built outline**, and earlier work is **carried forward** from the previous snapshot.

- `read_history` adds `outline`: when events are omitted, the omitted span is cut into at most 20 equal chunks. Each has the event range, first and last time, counts by kind, failed tool calls (count and up to 3 clipped, redacted texts), compactions, and the last assistant line (120 characters). Nothing in it is model-written. It is bounded (about 10 KB), so the existing 400/200/100/50 shrink loop still applies.
- `read_history` adds `counts` over the full event list (`tool_calls`, `tool_failures`), so numbers do not depend on the trim.
- `read_history` adds `previous_snapshot` (read only): the number, time, event and prompt counts of the last snapshot and its report text (clipped to 6,000 characters). The skill treats that report as the baseline for what happened up to then, covers everything after it from the events, and states what changed. Whole coverage is therefore the outline (the middle of this snapshot) plus the previous report (everything before it), never silent omission.
- The skill says so: it must not claim coverage of an elided span except through the outline or the previous report.

## Snapshot store

`<report>.snapshots.json` next to the cached report (`~/.cache/herdr-engineering/summary/<agent>-<session>.snapshots.json`), mode 0600 in a 0700 directory, written by `publish` only, atomically. `summary history` and the viewers only read it.

- A snapshot is `{n, taken_at, prompts, events, tool_calls, tool_failures, compactions, agents, worked_s, fresh, cached, segments, drift, coverage, report}`. Everything except `report` is computed by code from the history (`worked_s`, `fresh`, `cached` are `null` when not recorded, never 0; `coverage` is the history helper's own verdict, not the report's text; `segments` and `drift` come from the validated drift graph, `null` when there is none).
- `report` is the redacted report text (the same text that was posted). Only the newest 5 keep it.
- Bounds: 200 snapshots (the first is kept, the oldest after it are dropped) and about 400 KB. Every number passes `clean_point`, which whitelists keys and types.
- A history that cannot be read records no snapshot and says so in the publish result (`snapshot: {ok: false, error}`). The report is published as before.

## Plot

`snapshot_plot.py` (standard library, pure `render_snapshot_plot(snapshots, width, unicode, color) -> lines`) draws one labelled sparkline row per metric: prompts, events, failed tool calls, time worked, fresh tokens, drift segments, plus an evidence row (`c`/`p`/`l`) and drift markers. Each row ends with `first → last`. Snapshots are spaced by index, not time; the heading gives the first and last time and the count. Narrower than the sparkline floor the rows degrade to `first → last` text. `view_report` appends it after the rendered report (re-read on every redraw, so a republish refreshes it). It is shown from 2 snapshots; with one it shows a one-line note.

Dashboard: the control plane stores the snapshot's whitelisted numbers on the `SummaryReportCompleted` event (no new store: the event log is the history), and `GET .../summary` returns `full_report.snapshots` (last 200, oldest first). `packages/summary-snapshot-plot.js` turns them into an SVG with small multiples (same metrics), rendered under the report.

## Anti-goals

- A number in the plot that code did not compute. A secret in the store (redaction and the whitelist). A world-readable store. `summary history` writing anything. An unbounded store.

## Decided by me (change if wrong)

- Index spacing, not time spacing, on the x axis.
- Failed tool calls instead of test pass/fail.
- Keep report text for the newest 5 snapshots only.
