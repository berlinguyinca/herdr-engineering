# UX and visual contract

## Non-negotiable
The glowing, live, beautiful **activity map is the hero**, not a static diagram, metric board or log list. Preserve existing Herdr styling and information architecture; adapt design references rather than cloning them.

## Primary surfaces
1. **Workspace pane:** dockable, resizable live activity map; task and agent selection synchronized with existing terminal panes.
2. **Room view:** membership, humans and agents, messages, delegation, ownership, review and cross-room collaboration. Clicking an edge opens the authorized original room thread.
3. **Agent Operations full page:** large zoom/pan map, task inspector, filters, host overview, replay timeline.
4. **Terminal extension:** colored Unicode/character-cell map, activity pulses, focus and keyboard navigation, compact/narrow fallback. Never require browser for essential inspection.
5. **Work Command Center:** embed task execution lineage, accountable owner, contributors, reviewer, artifacts, tests, verification status.
6. **Fleet:** cross-host overview using existing Herdr host metadata.

## Visual semantics
Agent node = real agent/session; inner label = model/provider; outer ring = known context utilization (unknown displayed explicitly); node halo = activity state. Colored/animated directed edges = communications, handoffs, reviews; solid vs dashed distinguishes accepted vs planned. A model change updates the same agent node unless a new session actually starts. Room clusters show membership, not task ownership. Use non-color labels and accessible descriptions.

## Interactions
Map mode: Activity / Communication / Handoffs / Ownership / Replay.
Filters: room, workspace, task, host, provider, model, event kind, time.
Click node -> agent/session inspector; click edge -> event/handoff/thread inspector; click task -> Work Command Center; double-click/open -> existing pane. Replay uses timestamp scrubber, speed and pause; Live resumes current state.
Respect reduced motion, keyboard navigation, screen readers, high-contrast and responsive breakpoints.

## Visual assets
01 = integrated workspace; 02 = room collaboration; 03 = terminal; 04 = full-page operations. Use as layout and hierarchy guidance, not literal component inventory. Sample metrics and model labels are illustrative only.

## Required before/after evidence
Capture actual current frontend screenshots in local development environment after discovery. Record baseline and proposed views at desktop, tablet and phone widths; record terminal 120x40 and 80x24. Do not label generated concepts as actual screenshots. Produce a visual comparison and iterate.
