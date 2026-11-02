# Dashboard UX contract

Use existing Herdr design system, app shell, navigation, API client, accessibility, pagination, error states, and contextual views. One top-level `Memory` destination; don't create a new SPA or database.

## Routes / information architecture (proposed; adapt to current routing)
- `Memory / Explorer`: search across authorized banks, keyword/semantic mode, typed filters (project, mission, kind, status, date, origin harness/host), saved filter URL; paginated real results with source and freshness. Empty / no access / backend offline are distinct.
- `Memory / Detail`: side drawer then permalink with full text, status, revision, timestamp, bank, evidence links, created-by actor, retrieved-by events, supersession history and correction actions if authorized. Never fabricate scores or provenance.
- `Memory / Graph`: relationship display only for existing explicit Herdr graph/event edges; empty state with explanation if none. Time filter and link to source event. No invented semantic edges.
- `Memory / Timeline`: memory created, retrieved, promoted, corrected, retracted, handoff referenced, filter by mission/session, link existing event stream.
- `Memory / Integrations`: host × harness × adapter version with installed/configured/reachable/verified distinct states, last probe, drift, canary controls; never show token values.
- `Memory / Quality`: candidate queue, stale/conflicted/superseded proposals; decisions audited; features hidden until backed by real processors and metrics.
- Existing mission/agent/PR/handoff views: contextual `Memories` tab and `Why this memory?` link to detail/retrieval event.

## Style
Follow current `DESIGN.md` and `packages/herdr-design-system/` tokens: restrained neutral UI, semantic colors, table/drawer patterns, no fake hero KPIs. Desktop 4K responsive; mobile usable. Keyboard navigation, screen reader labels, pagination and loading/error skeletons. Every memory type/status is text-labelled and not only color.

## First PR minimum
Read-only Explorer + Detail + integration status. Search result links to source event when present. No graph/quality/replay demo data in shipped UI. Hide unsupported actions behind feature flags and show real server errors.

## Later phases
Verification/supersession workflows, memory-health trends, explicit relationships graph, controlled as-of replay with immutable retrieval snapshots, incident analysis. Never mix observational correlation and causal proof.
