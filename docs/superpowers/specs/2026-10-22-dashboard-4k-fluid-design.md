# Dashboard 4K Fluid Layout Design

## Intent

HerdR operators primarily use 4K displays. The deployed shell currently caps every route at 1600px, leaving roughly 1960px unused at 3840px. Operational evidence should occupy the available canvas while prose remains easy to read.

## Goals

- Use nearly all width after the sidebar at 2560px and 3840px.
- Let Overview expose more independent panels in the first viewport.
- Let grouped catalogs place independent groups beside each other on very wide screens.
- Give CI heatmaps, pipeline graphs, tables, and project inspectors the full operational canvas.
- Shorten repeated CI project labels while preserving exact provider identity accessibly.
- Preserve current mobile and tablet behavior, routes, refresh state, and keyboard access.

## Non-goals

- Larger typography or scaled-up decorative cards.
- Stretching prose introductions across the display.
- A draggable dashboard or user-configurable grid.
- Changing telemetry, retention, or metric definitions.

## Approaches

1. Increase the fixed ceiling to 2400px. Simple, but still wastes about one third of a 4K display and requires another arbitrary ceiling later.
2. Remove the operational ceiling and add wide-screen component layouts. **Chosen.** It gives data-heavy views the canvas while each component controls readable internal widths.
3. Scale the entire application with viewport width. Rejected because larger text and controls reduce information density and do not improve comparison.

## Layout

The app shell uses the full width after the sidebar with a responsive outer gutter. Text introductions retain their existing `ch` limits. At 2200px and above, Overview uses four columns with missions, fleet, hosts, and activity each spanning two balanced columns. Agent project groups use a two-column grid, while rows within each group retain their existing readable structure. Chronological Activity remains a bounded single flow; Services and Topology use two independent group columns; Analytics bounds its linear stage chart. Projects widens its repository rail and gives selected issue detail more space. CI inherits the full canvas for heatmaps and pipeline graphs.

The visible CI heatmap row label shows the repository name only. Provider URL and repository ID remain in cell accessible names, exact-count tables, tree summaries, search, and diagnostics.

## Verification

Browser tests cover 3840px shell width, four-column Overview placement, two-column agent groups, readable prose width, no document overflow, and unchanged 390px behavior. CI unit tests prove compact visible labels retain full identity in accessible text. Capture live 3840×2160 screenshots for the main operational routes.
