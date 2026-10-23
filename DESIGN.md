# Design

## Source of truth
Status: Active. Date:2026-10-22. Surfaces: HerdR dashboard and embedded operational views. Existing canonical tokens and component rules remain authoritative. Evidence: packages/herdr-design-system/README.md, theme.css, app-shell.js, route/view sources, responsive browser captures and docs/evidence/dashboard-space/audit.md. No prior root DESIGN.md existed.

## Brand
Retain light neutral surfaces, green brand actions, restrained borders and semantic outcome colors. Trust comes from exact identities, provenance and visible freshness/coverage. Avoid decorative cards, giant counters and artificial data used to fill space.

## Product goals
Show more relevant work in the first viewport, reduce scanning/scrolling, and keep selected detail beside its context where space permits. Non-goals: new telemetry, metric redefinition, a theme replacement, or a draggable dashboard. Success signals: usable content width, first-row position, visible record count and preserved readability.

## Personas and jobs
Engineering operators inspect agents/missions, find CI failures, compare hosts and open logs/conversations. Desktop supports parallel inspection; mobile supports triage and focused detail. This prioritization is inferred from current workflows and the user's request to use space better.

## Information architecture
Preserve current routes and deep links. Proposed CI subviews: Health, Runs & pipelines, Tests. Services retain explicit ownership scopes. Navigation grouping and drawer are proposals; do not remove routes. Overview prioritizes attention, current work and CI, with compact fleet context.

## Design principles
Use width according to content; preserve readable text. Reduce repeated framing before reducing font size. Prefer compact rows for collections and cards for meaningful grouped summaries. Disclose secondary metadata while preserving access. Empty space from unavailable data is distinct from wasted layout.

## Visual language
Reuse theme.css colors, font families, radii and motion. Proposed gutters24px desktop/16px mobile, panel gaps12–16px; toolbar/row spacing should be coherent. Preserve text labels alongside colors. No new illustration or icon dependency.

## Components
Reuse shell, existing tabs, trees, inspector and table semantics. Proposed shared page header, filter toolbar, compact row variant and bounded list/inspector layout. Active shell owns styles in Shadow DOM; components.css alone cannot change it. Density preference may be persisted once its behavior is approved.

## Accessibility
Target WCAG2.2AA as proposed acceptance standard. Preserve visible keyboard focus, semantic tables/disclosures and status text. Ensure drawer/split controls have accessible names and focus handling. Touch targets remain adequate even when desktop rows become compact. Honor existing reduced-motion tokens.

## Responsive behavior
Operational views use all available width, including 2560px and 3840px displays; prose remains constrained inside them. Above2200px, independent operational groups may form additional columns while ordered timelines remain single-flow. Split panes depend on usable container width, with full-width selected detail on small screens. Validation widths3840/2560/1920/1440/1280/768/390. Mobile is single-column with accessible navigation and expandable filters. Tables/heatmaps may scroll internally; document must not overflow horizontally.

## Interaction states
Keep loading distinct from empty/unavailable/stale/partial. Preserve provider coverage, history limitations and freshness. Refresh must preserve focus/selection/expansion and input state. Empty screens should omit inapplicable group actions while keeping meaningful filters discoverable.

## Content voice
Short operational language; lead with entity and state. Put verbose technical IDs, provider origins and repeated explanations in details or help. Preserve exact values in accessible detail and copyable content.

## Implementation constraints
VanillaJS/Web Components, existing CSS tokens, no new dependencies. Preserve route/API/data contracts and test report durability. Implement incrementally only after a chosen design; capture responsive and interaction evidence for each phase. This audit changed no production UI.

## Open questions
- [ ] Operator: should a user-selectable comfortable density be added later? Compact desktop density is now the default; mobile retains larger spacing.
- [ ] Designer/implementer: evaluate container-query splits for populated Projects data when GitHub synchronization is available.
