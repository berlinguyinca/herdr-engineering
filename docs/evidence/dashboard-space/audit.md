# Dashboard space audit — 2026-10-22

Read-only live inspection of http://dev.lan, release f7f7bdbd35ad. All 14 primary destinations inspected, plus analytics subpages, bender host detail, and a current agent detail. Screenshots at 1920×1080; Overview additionally at1440×900 and390×844. Raw screenshots and route inventory are local ignored artifacts under artifacts/dashboard-space/. Live records change during capture; numbers describe samples, not invariants. No product code or production configuration changed.

## Findings, ranked

1. **Shared width ceiling.** theme.css sets content-max1080px and sidebar232px. Actual Shadow DOM shell applies centered max-width plus24px padding. At1920px the main region is1688px; content outer box1128px;560px sits in side gutters (33.2% of main). Proposed available inner width1640px with existing sidebar/padding is52% wider than1080px. At1440px the gain is only7.4%; vertical layout changes matter more there. Do not simply stretch prose or rows to fill a screen.
2. **Overview hierarchy.** Sample page2115px tall. Missions and Fleet span both columns. Mission list pushes CI below the first screen; Fleet occupies a whole row for three small metrics. Use a fleet/status strip, primary attention/work area and secondary CI/agents/reviews column. Independent column flow should avoid stretched empty cards. Keep counts' existing freshness semantics.
3. **Catalog row density.** Agents sample3260px for20 displayed records; individual visible rows approximately130px. Missions sample2061px. Multiple ID/model/activity/host lines and14px padding inflate each row. Propose compact desktop rows with title/activity plus aligned host/model/status/age; move raw IDs and long metadata to disclosure/inspector. Aim72–88px typical rows, with natural growth for long meaningful content; maintain readable text. Standard density remains available.
4. **Control height.** Topology, Missions and Services place the first data row around380–420px below page top. Several layers repeat descriptions, metrics, tabs, search, count and group actions. Combine heading/count/freshness, a compact filter row and one optional advanced-filter disclosure. Target first data within240–280px at900px-high desktop; verify real keyboard/touch behavior.
5. **CI information architecture.** Sample page3713px even before a selected log. Heatmap/tree, legacy run filters, test-report filters, charts and lists occupy one document. Use Health / Runs & pipelines / Tests workspaces with shared explicit context. Preserve all five windows and distinct history/report coverage. Health keeps a bounded heatmap; selected run opens beside a bounded tree on wide screens. Put provider origin in tooltip/details instead of repeating its URL in every heatmap label. Do not accidentally link independent filter semantics or alter denominators.
6. **Activity feed.** Sample50 events8316px tall. A record is a padded card with a long mostly raw metadata string. Use timeline/table rows for time, event, subject, host and concise summary; expand original payload per row. Target48–64px typical collapsed row. Preserve order and every event; no silent collapsing of meaningful changes.
7. **Projects.** Nested repository sidebar consumes180–240px inside capped content. Use collapsible repo navigation and issue/inspector split based on actual container width. Live connection needs server configuration, so populated issue layout could only be assessed from code; do not classify the empty page as a padding defect.
8. **Hosts.** Existing version matrix is relatively space efficient. Align host summary metrics above it, keep sticky host/component headers, allow horizontal scroll when needed. Do not compact away drift/staleness evidence.
9. **Services / Topology.** Preserve useful trees. Compact collapsed group rows and reuse a list/inspector pattern on expansion. System services currently empty: hide inapplicable expand/collapse controls, retain useful search/filter state and clear empty-state meaning. Do not invent occupancy to fill the page.
10. **Analytics.** Home has only four metrics and two stage bars; model/host/token pages are separate. Compose existing metrics into useful adjacent panels and tabs. Token tables need proper shared table spacing and numeric alignment. Show limited-data coverage honestly; no fabricated trends or new telemetry scope.
11. **Collaboration and agent workspace.** Room landing page is sparse and has an always-visible create form. Prefer room list/navigation plus selected conversation; creation can be an explicit action. Agent panes are75vh with480px minimum below surrounding sections, creating nested/outer scrolling. Use remaining viewport height and collapsible summary/context; keep composer reachable. Do not shrink message text or logs.
12. **Mobile.**390px Overview consumes about130px in brand and horizontal navigation, before title/intro; first mission title starts around420px. Use a compact brand/route header and accessible navigation drawer,16px gutters, single-column cards and expandable filters. Desktop compact density must not shrink mobile touch targets.

## Approaches

- Width-only patch: fast and useful on large displays; leaves stacked controls, tall rows and long CI/Activity pages. Insufficient alone.
- **Recommended: adaptive operations layout.** Shared fluid shell for operational views; readable inner widths for prose; compact consistent toolbars; list/inspector patterns and task-specific CI tabs. Incremental and testable.
- User-configurable tile dashboard: potentially flexible but adds persistence, drag/drop and accessibility complexity before fixing current information hierarchy. Defer.

## Suggested sequence and proof

1. Shell width and shared header/filters; Overview reflow.
2. Agents/Missions/Services row density, Activity timeline.
3. CI workspaces and container-aware project/tree/inspector splits.
4. Analytics composition, Collaboration/workspace height and mobile navigation.

Targets above are proposed acceptance criteria, not measured improvements. Capture before/after at1920×1080,1440×900,1280×800,768×1024 and390×844. Assert no page overflow, visible focus, preserved selection/expansion/filter state under refresh, correct split/list navigation, reachable composer and unchanged data/freshness semantics. Compare first-row position, visible records, scroll distance and content width. Existing tests should remain green; add behavioral tests only for changed interactions. No dependencies needed.

## Implemented measurements

The adaptive layout implementation was captured with the real local control-plane browser fixture at1920×1080,1440×900,1280×800,768×1024 and390×844. The shell content box measures1648px at1920px, up from1128px in the live baseline (**46.1% more horizontal canvas**). It measures1207px at1440,1047px at1280,535px at768 and390px at390. Every measured viewport had document scroll width equal to viewport width.

Fixture Overview height at1920 fell to1104px from the live-baseline2115px sample, although record counts differed and this number is directional rather than a controlled performance metric. The revised fixture pages measured: Agents1080px, Missions1080px, Activity2112px for50 expandable records, Analytics1080px and default CI Health1080px. The live Activity baseline was8316px for50 card records. Responsive screenshots were visually inspected under `artifacts/dashboard-space/after/`.

Populated CI captures cover the 30-day cross-project heatmap and PR tree, a selected failed pipeline with dependency graph, failing step log and test rows, the Tests workspace with trends and recurrent failures, plus mobile Health and pipeline layouts. The refreshed 768px Overview capture confirms a single content column beside the desktop sidebar, avoiding the earlier narrow two-card wrap.

Implemented scope: fluid shell, three-column Overview with an earlier tablet collapse, compact Agents/Missions/Services/Topology controls and rows, expandable Activity timeline with bounded original payload, remaining-height agent workspace, keyboard-operable CI Health & pipeline/Runs/Tests workspaces, Analytics destinations and collapsed mobile navigation with safe focus return. Projects keeps its existing collapsible repository navigation; a populated issue split remains an open evaluation because the inspected deployment reported GitHub synchronization unavailable. Collaboration keeps its established room layout; wide shell space is available without inventing content for a sparse room list.
