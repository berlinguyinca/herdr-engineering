# 0150 — Unified Engineering UX

## Goal

Combine the capabilities built so far into one coherent, layered experience rather than a collection of plugin tabs.

## Information architecture

Primary navigation should converge on:

- **Fleet** — machines, connection/health/capabilities;
- **Sessions** — agents and CURRENT/TIMELINE/SUMMARY/RELATED;
- **Missions** — semantic Pi/AutoSpec grouping and candidates;
- **Previews** — dev services/browser evidence;
- **Tests** — local/worktree test tree/history;
- **CI** — pipelines/queues/runners;
- **Artifacts** — shared evidence/handoffs;
- **Attention** — blocked/approval/failure items.

Avoid duplicating the same session list in unrelated plugin pages. Existing plugin UIs may be embedded/deep-linked but should inherit shared navigation/context when possible.

## Shared context bar

Where space allows, show machine → repository → worktree/branch → session/mission, with direct navigation. On phone, collapse progressively while retaining identity and attention state.

## Search/goto

Provide cross-surface search/goto by machine, repo, issue/mission, session, worktree, branch, test, CI run and artifact. Search results navigate to canonical owning views.

## Performance

- Lazy-load heavy logs/browser views.
- Virtualize long timelines/test lists.
- Avoid polling every machine independently from every browser tab; centralize/subscription-cache where supported.
- Mobile remains responsive with large fleets and many sessions.

## Accessibility

Keyboard navigation on desktop, touch targets on mobile, semantic status labels beyond color, and readable log/test failure rendering are required.

## Exit gate

A usability walkthrough can complete the five master user journeys without navigating unrelated standalone tools manually, while deep links remain stable and phone/tablet layouts preserve core steering/review functions.
