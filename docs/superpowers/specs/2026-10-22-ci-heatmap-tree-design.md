# CI heatmap and run tree — draft design

Status: approved by the user, 2026-10-22. This extends the deployed CI insights design. The reference wireframe uses illustrative data; implementation is tracked in the accompanying plan.

## Intent and selected approach

Give operators an overview of failures across every CI project, then a short path from a problem cell to its PR, run, pipeline step and log. The user selected a daily outcome heatmap and added hourly, last-24-runs, and PR tree views. Default-branch filtering applies across all five heatmap modes.

Considered: daily/hourly mixed-outcome cells (selected; failures remain visible), failure-rate gradients (can hide individual failures), latest-only daily status (hides earlier failures). A separate tree-only overview loses cross-project comparison; deeply expanded trees waste space. Use the heatmap above a compact tree and existing pipeline inspector.

## Heatmap contract

Modes: trailing 24 hours split at UTC hour boundaries; trailing 24 distinct pipeline runs per project; 7, 14 or 30 calendar days including today. UTC is explicit. Hourly mode covers the exact trailing 24 hours, partitioned by UTC clock hour. The first and last buckets may be partial, producing up to 25 visible hour cells; label partial ranges precisely in tooltips and selection details. Never silently shorten the range to 23 full hours plus the current partial hour. Day/hour modes align timestamps across projects; last-runs mode aligns ordinal positions only, with newest at right. Last-runs mode ignores a date cutoff and labels that difference. Fewer than 24 runs are left-padded with empty cells. Use compound provider-instance/repository/run identity; show full repository names, adding forge/provider context wherever labels could collide. Deduplicate provider run identity; test report attempts are not additional runs.

Bucket pipeline runs by creation timestamp and use each run's current authoritative outcome. Count success/failure separately from canceled, skipped, running, queued and unknown; never classify cancellation alone as failure. Green means completed success with no failures, red means failures with no successes, amber means both. A blue marker indicates active runs without hiding completed failures. Neutral canceled-only/unknown-only cells are distinct from no runs. Incomplete coverage is explicitly marked; a gray no-run cell requires known coverage. Hover, keyboard focus and selection expose exact counts and dates. A run cell exposes run number, branch, revision, actual outcome and duration.

Default scope: builds on the provider-reported default branch, excluding PR events; do not infer the default branch if unavailable. Explicit scopes include pull requests and all runs. The PR tree is visible when its scope includes PR events; otherwise offer an explicit Include pull requests control. Grouping must never silently broaden the selected scope. Preserve filters and selection across refresh and navigation.

Every project appears, including inactive projects. Search and optional failures-only filtering are explicit. Sort by failures or project name, keep rows stable during interaction, and state the sorting basis. Aggregate statistics state successful and failed denominators and exclude unfinished/canceled/skipped outcomes.

## Tree and drilldown

Hierarchy: project → PR or branch → run → workflow → step. Initially show project/PR-or-branch/run only; fetch workflow/step details on expansion. Separate non-PR builds into their actual branches. Identify PRs only through explicit provider metadata, a validated PR ref, or an exact repository-scoped forge PR URL; never guess from branch names. If no reliable association exists, show PR identity unavailable with its run. PR title and source→target appear only when actually reported; otherwise show PR number and reported branch without invented roles.

PR and branch rows show latest authoritative run status separately from failures in the selected window, e.g. Latest passed · 2/8 failed. Parent state must not suggest that an earlier failure is still the latest status. A mixed day stays mixed even if its latest run passed. Repeated executions are separate only when the provider distinguishes them; report attempts remain within their run.

Selecting a heatmap cell scopes the tree to its project and bucket and displays a removable selection chip. Selecting a run opens the existing pipeline inspector alongside the tree on wide screens, or below it on smaller screens. Selecting a workflow step opens the existing log panel. Breadcrumbs preserve project/PR/run/step context. Tree-only and pipeline-only expansion controls allow full width; remember expanded nodes without forcing all 117 projects open. Window totals and latest-state labels use the same scoped run set and explicitly label when latest means latest within the selection.

## Space, accessibility and visual language

Reuse existing HerdR tokens, native components and red/green/blue status colors. Use symbols and text as well as color. Heatmap headers/project names stay sticky inside a bounded scroll region. Desktop fits 30 daily columns where possible; mobile scrolls only the grid and stacks the tree/inspector. Aim for 32–36px project rows, 22–28px cells, and a user-expandable overview height. Touch targets may be larger than visible cell fill. Keyboard navigation, focus indicators, accessible expanded state and an exact-count table alternative are required. A stale or partially loaded snapshot remains clearly labeled.

## Data and verification constraints

Existing code retains bounded observations (2,000 runs) and recent provider pages, not a complete 30-day archive. Before presenting complete coverage, add bounded paginated backfill plus per-project coverage/watermarks. Last-24-runs also needs pagination when filtering removes recent rows. Display incomplete windows while backfill runs; distinguish provider errors, truncation, unavailable timestamps and no activity. Summary counts must survive retention without claiming raw run details beyond retention. A backend aggregate/coverage store may be needed for busy projects; validate volume before selecting storage changes. Do not fetch logs or details for every tree node.

No new runtime dependencies. Verification must cover time boundaries, current partial bucket, DST-independent UTC, mixed/canceled outcomes, multi-forge identity collisions, PR refs and missing metadata, retry/report deduplication, sparse histories, pagination/coverage and stale/error states. Browser checks cover keyboard interaction, filter synchronization, refresh preservation, 117+ projects, 30-day width, mobile overflow, and red failures. The accompanying SVG is illustrative, not production evidence.

## Open design choices

- Proposed: expose PRs through an explicit PR/all-runs scope; retain default-branch builds as the default across modes.
- Proposed: project/PR tree and selected pipeline share the lower desktop area, with full-width expansion available.
