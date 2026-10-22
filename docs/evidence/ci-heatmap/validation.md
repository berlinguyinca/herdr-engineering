# CI heatmap and tree validation

Approved design: `docs/superpowers/specs/2026-10-22-ci-heatmap-tree-design.md`.
Reference wireframe: `docs/wireframes/ci-heatmap-tree.png` (illustrative data).

## Behavioral proof

- Model tests cover all five windows, exact trailing 24h UTC edges, last 24 filteredruns older than 30 days, sparse/padded rows, mixed/canceled/unknown outcomes, identity deduplication, explicit PR refs and repository-scoped PR URLs, missing timestamps/default branch, retained coverage, and pinned run selection across insertions/eviction.
- Background history tests cover bounded pagination/caps, exactly 50-repository Woodpecker snapshots, repeated/overlapping/unordered pages, mutable nonterminal outcomes outside the head, deleted rows shifting cursor boundaries before/during page reads, head mutation, stale cache expiry, successful rescan pruning, per-repository classified errors, restart reset, and budget continuation across 117repositories with simulated 200ms provider reads.
- Flat history association tests reuse exact forge/repository/full-commit links for mission/session and canonical repository routes without mutating source snapshots.
- Client tests use a real local HTTP server to verify viewer-readable history GET and cancellation.
- Browser integration uses the real ControlPlaneServer and WoodpeckerProvider against a local TLS protocol server. Inventory explicitly asserts 117 rendered projects. Default-scope last 24 includes old provider runs beyond 30 days, and selecting an older run opens real provider workflow/step/log endpoints. Tests cover all five windows, explicit PR scope, red failed-step styling, selected-run highlighting, refresh focus/expansion, desktop 30-column fit, mobile no page overflow, keyboard selection, preference reload, and canonical repository/search deep links.
- Controller browser tests use an actual local HTTP server, verify lazy expansion/step callbacks and disposal cancellation, and retain stale heatmap rows under a provider-reported outage.

## Independent review and regression findings

Independent code and architecture lanes reviewed the implementation. Their concrete findings were repaired with regression coverage: contextual associations, compound inspector identity, filtered/pinned run selections, safe provider diagnostics, declared snapshot inventory, offset continuity, immutable-versus-mutable cache refresh, retained-row pruning and load-budget progress.

An unrelated existing inspector browser test matched the permanent Reasoning tab while body content still read Loading. The test, controller and renderer were byte-identical to baseline a203cb7. Only its readiness wait was scoped to the actual empty-state body; its final assertion remains unchanged. The original failure was reproduced in isolation and in the full JavaScript run before this correction.

## Visual proof

Generated, ignored artifacts: `artifacts/ci-health/desktop.png` and `mobile.png`.
The desktop acceptance additionally asserts all 30 daily columns fit inside the 1440px viewport’s overview. Full ISO dates remain available to assistive technology/tooltips; displayed daily headings use compact month/day labels. Mode buttons distinguish the active selection. The 117-project grid has sticky headers/project labels, bounded vertical height and user resize. Closed tree groups do not render run buttons; expanded groups page 100 runs without changing parent totals.

## Scope limits

Provider history is a bounded, memory-only cache rebuilt after restart. Coverage labels remain incomplete under errors, caps or unavailable metadata; the separately durable JUnit store is unchanged. Serialized-row byte limits do not represent total Python/browser memory overhead. This is native provider execution evidence; no synthetic test reports are published to production during acceptance.

## Integrated results

- Latest-base full JavaScript/browser suite: **331 passed** (`/tmp/herdr-heatmap-node-release.log`).
- Cold-start loading copy follow-up: **22 focused model/controller tests passed** (`/tmp/herdr-heatmap-loading-green.log`).
- Latest-base full Python suite: **2,247 passed, 6 skipped, 1 xfailed** in 597.20s (`/tmp/herdr-heatmap-python-release.log`).
- Ruff, JavaScript syntax and diff checks passed. Independent code review: APPROVE; architecture: CLEAR.
- Live release proof follows after deployment.


Main advanced with PR151 (summary-server Compose bounds only) after the full suites. It was merged without conflicts before release; **59 focused Python tests** (including the updated summary deployment contract, CI history/API and correlation) and **22 model/controller tests** passed on the combined tree.
