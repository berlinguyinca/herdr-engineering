# Operations Workspace Design

## Intent

Turn the fluid HerdR dashboard into an operator workspace that prioritizes actionable failures, remains readable on 4K displays, and lets operators tune information density without losing exact provenance or responsive behavior.

## Evidence and constraints

Live routes were inspected at 3840px after deployment `5ec60af`. Projects uses the wide canvas effectively once an issue is selected. Overview remains sparse when feeds are quiet, CI briefly exposes independent refresh phases, and long project lists require excessive document scrolling. Existing CI retained-history and trend models already cover refresh preservation, test history, duration, and cache regression; they need clearer presentation and regression evidence rather than parallel implementations.

Use vanilla JavaScript and current Web Components, tokens, APIs, routes, and storage patterns. Add no dependency. Preserve keyboard focus, exact provider/repository identity, stale/partial/empty distinctions, mobile containment, and chronological order.

## Chosen approach

Build one progressive operator workspace on existing route data. Overview derives an attention queue from its nine already-polled sources. The shell owns a persisted Comfortable/Compact/Dense preference. Overview owns persisted card visibility and ordering through an explicit customize disclosure rather than drag-and-drop. Projects uses sticky, independently scrolling panes on wide screens. CI keeps its current retained snapshot and trend models, adds collision-only provider disambiguation, and improves its pipeline inspector with successful-step collapsing, zoom, and bounded two-run comparison.

Alternatives rejected:

- A separate attention backend would duplicate already-polled dashboard state and introduce consistency gaps.
- A free-form draggable dashboard would add pointer, keyboard, persistence, and collision complexity without improving incident triage enough.
- Rebuilding CI history or trends would fork established semantics and retention limits.

## Interaction model

- Attention items are derived, severity ordered, deduplicated, and linked to the exact existing route. Partial sources never produce false zeroes.
- Density changes row padding, gaps, and metadata visibility while preserving readable type and mobile touch targets. It applies immediately and persists locally.
- Overview customization offers Move up, Move down, Show, Hide, and Reset actions. Attention stays visible and health remains a footer.
- At desktop widths, Projects repository, issue, and inspector panes scroll independently within the viewport. Smaller widths return to document flow; selected detail becomes a focused mobile panel with a sticky Back control and restored list scroll.
- Duplicate CI repository names gain the shortest provider-host suffix that distinguishes them; unique names remain compact.
- Successful pipeline steps may be collapsed as a group. Zoom changes graph scale without document overflow. Compare lets an operator pin a second observed run from the same repository and displays state, duration, revision, and step outcome differences. Logs remain tied to the actively selected run. Wide CI uses a run/tree rail beside a sticky inspector, and the heatmap preserves minimum cell width with internal scrolling and sticky identity/time headers.
- Existing CI trends remain filter-consistent and visibly qualify retained, partial, stale, or memory-only coverage.

## Responsive composition

Validate 3840, 2560, 1920, 768, and 390px. The persistent sidebar collapses by 900px so content remains usable at tablet widths. Wide panes use available height and width; prose and chronological streams stay bounded. Density controls collapse to a labeled select on narrow screens. Overview customization uses a disclosure and one-column action list on mobile. Pipeline comparison stacks below the active inspector under 1250px. Activity keeps the chronological stream on the left and uses otherwise empty wide space for event-type filters/summary and selected-event detail. Analytics uses an auto-fitting operational grid and never forces document overflow.

## Operational repair

Repair the gateway's missing Certbot live/archive source only after identifying why it disappeared. Preserve the currently valid copied edge certificate and healthy collaboration containers. Prove renewal dry-run and a full deployment including collaboration services before considering the repair complete.

## Acceptance

- Operators can see and open cross-project attention without scanning every card.
- Density and Overview preferences persist, sanitize malformed storage, and remain keyboard usable.
- Projects panes retain selection, focus, and independent scroll through refresh.
- CI labels remain compact and unambiguous; refresh never replaces retained data with a false empty state.
- Pipeline controls preserve dependency truth and compare only observed runs.
- No tested viewport has document-level horizontal overflow.
- Full tests, independent review, live screenshots, exact-SHA deployment, and gateway health all pass.
