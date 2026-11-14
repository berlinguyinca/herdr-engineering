# UI design, existing Herdr conventions

1. Existing Overview `#/overview`: insert a responsive `This week` card below existing Attention, with small metrics (Started / Finished / Open / Quota-paused), coverage disclaimer, rows sorted by attention and recency, and filter chips. Keep existing overview cards and CSS tokens. A partial source must not report zero as a reliable total.
2. Expanded item: title, status, stage, measured progress if any, next action, last event, session count and source breadcrumbs. At most one click to canonical web detail.
3. Existing session view `#/herd/session/:id`: add a compact Work Context header linking to parent mission, GitHub/PR when verified, progress, next action, and its related sessions. Existing conversation/activity split stays.
4. Existing mission view `#/missions/:key`: keep story, timeline, reviews, related panel and tested progress; do not replace.
5. Existing GitHub board `#/github/projects`: cards retain external GitHub source link; show `Open in Herdr` **only** if a verified target exists; otherwise no dead link.
6. Existing related-panel: add a Work/Project context row only if grounded and authorization permits.
7. Native terminal pane: link to web session and separate `Terminal` control using existing `getTerminalAttach`; never claim browser-focus of remote Herdr pane.
8. Progressive enhancement: compact small screens, keyboard focus retention, reduced motion, no fabricated progress, accessible `<progress>` label, loading/empty/stale/error states.
9. No new top-level navigation required for WC-001; later `/work` may be added after metrics are trusted. Preserve query/filter state on return navigation.
10. WIP coaching and cross-tool requests are NOT in WC-001; subsequent slices reuse Rakazo assistant and Attention mechanisms.
