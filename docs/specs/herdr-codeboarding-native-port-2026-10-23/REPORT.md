# Implementation report — Architecture Explorer, slice A + B + C (+ partial D01)

Branch `feat/architecture-explorer` off `origin/main` 8c29a13e. Not complete: phases D02–D03 and E are NOT done.
Nothing was deployed; the feature flag is off by default.

## What was built
| Task | Status | Where |
|---|---|---|
| A01–A04 audit | done | `audit.md` |
| B01 vendor upstream | partial, deliberate | `herdr_engineering/architecture/_vendor/codeboarding_content_hash.py` (MIT, pinned 647fe6a7, notices in `THIRD_PARTY_NOTICES.md`). Only pure hashing is vendored. The `ast` import/symbol extractor is **new code**: upstream's engine needs a language server and emits no import edges. |
| B02 isolated analyzer | done (one gap) | `runner.py`, `worker.py`, `gitsource.py`: `python3 -I` child, scrubbed env, rlimits (CPU/AS/FSIZE/NOFILE), wall timeout + process-group kill, cancellation, output cap, reads git blobs of a fixed commit with hooks/fsmonitor disabled, never executes analysed code. **Gap:** network namespace isolation (`unshare -rn`) is used only where permitted; on bender it is not, and the snapshot reports `network_isolated: false`. The worker performs no network I/O. |
| B03 normalize | done | `extract.py`, `identity.py`: nodes (repo/package/module/class/function), edges (`contains`, `imports`) with source span, extractor, confidence, `inferred:false`, diagnostics. External imports produce no edge. No `calls` edges. |
| B04 identity/dedupe | partial | Snapshot key = host, repo, commit, worktree, analyzer, config hash, toolchain hash; immutable (409 on conflict, idempotent on same graph). **No lease queue / job dedupe / debounce** yet. |
| C01–C03 UI | done for Python | `packages/architecture-view.js`, `architecture-layout.js`, `architecture.css`, `app.js` nav, client methods. Graph + accessible table, drilldown to symbol and file:line (GitHub blob link when repo is github.com), edge evidence, search, bounded cursor pagination, partial/stale/failed/running notices, snapshot diff panel, light/dark/mobile/4K. |
| D01 | partial | Snapshot-to-snapshot diff (API + UI). **No** GitHub PR/issue/CI overlay or PR link. |
| D02, D03, E01, E02, E03 | **not started** | |
| F01 | partial | tests, benchmarks, screenshots, notices, flag, rollback below. |

## Reuse
Event envelope + `record_event(require_durable)`, artifact store, `canonical_repository`, route/auth roles + Origin/JSON guard, fleet-config flag pattern + `_health` capabilities, `app.js` NAV/ROUTES, `HerdrClient`, `herdr-empty/-error/-spinner`, design tokens, Playwright harness style. `SnapshotStore` and `collab-flow.js` were evaluated and rejected (mutable cache; people/agent layered layout). No new service, SPA, graph DB, scheduler or chat.

## Interfaces
`GET /api/v1/architecture/{snapshots,analyses,diff}`, `.../snapshots/<id>/{children,nodes,nodes/<node>,nodes/<node>/edges}`; `POST .../snapshots` (operator or agent role, validated, ≤64 MiB), `POST .../analyses` (lifecycle events). CLI: `herdr-eng architecture analyze --repo . --url <cp> --api-key-file <f>`. Events: `architecture.snapshot.published`, `architecture.analysis.{requested,started,progress,completed,failed,cancelled}` on reserved streams `architecture_snapshot`/`architecture_job` (generic `/api/v1/ingest` refuses `architecture*`). `architecture.diff.completed` is not emitted (diffs are computed on read).

## Flag and rollback
Fleet config `control_plane.architecture.enabled: true` (strict keys; default off ⇒ routes 404, no capability, UI shows an explanatory empty state). No migration: snapshots live in the artifact store + event log, so rollback = set the flag false or revert the branch. Snapshots are held in memory and rebuilt from events at boot; graph bytes need the artifact blob (RustFS in production, in-process otherwise), so an in-memory-only control plane loses graph bytes on restart.

## Evidence
- Python: `pytest tests/architecture` 38 passed; full `pytest` exit 0.
- Node: `npm run test:client` + Playwright: `npm test` 401/402; the 1 failure is `tests/playwright/ui.test.mjs` "overview attention and saved layout…" — the file has the same 2 failures on a clean `origin/main` (reproduced), so they pre-date this work.
- Playwright `tests/playwright/architecture.test.mjs` 12/12 against snapshots produced by the real analyzer (disabled, empty, failed/running, partial, stale, drilldown with exact source line, search, diff, keyboard, dark/light × wide/mobile/4K without horizontal overflow, WCAG AA contrast on headings/body/node labels, and Herdr's own graph).
- Screenshots: `evidence/architecture-*.png`. Two defects (dark-mode headings, `app:null` source link) were found by looking at screenshots, not by the first test version, and fixed.
- Benchmark `evidence/benchmark-herdr-self.json`, Herdr @ this branch, 258 Python files, 4,932 nodes, 5,824 edges, 5.0 MB canonical JSON, largest child peak RSS ≈ 55 MiB. Analysis wall time is **not reliable**: bender's load average was ~100 on 24 cores during the final run (cold isolated median 6.2 s; an earlier, less loaded run measured 2.7 s median, 4.1 s max; warm in-process was not faster than cold in either run, so the cold/warm split is within noise). Publish+validate+store ≈ 0.2–0.3 s; query p95 < 10 ms. Incremental timing: not implemented.

## Acceptance gates (TESTS.md)
| Gate | Result |
|---|---|
| Full baseline graph of a representative repo, visible in Workbench, verified symbol→source links | **Pass for Python** (Herdr). JavaScript (92 files) and TypeScript are reported as unsupported ⇒ snapshot is `partial`. Pi is TypeScript: unsupported. |
| Same SHA/config ⇒ identical graph hash; parallel worktrees isolated | **Pass** (tests) |
| Full vs incremental parity | **Not done** (E01) |
| PR diff with provenance and uncertainty | **Partial**: snapshot diff + warnings; no GitHub PR linkage |
| Mission/worker/worktree/parent-worker/issue links; autospec and non-autospec | **Not done** (D02). Audit found no source for `issue_ref`, `room_ref`, `autospec_ref`, `commit_sha` on Pi events. |
| Unauthorized guests cannot view/analyse/comment | **Partial**: viewer/ingest roles cannot publish, anonymous refused, cross-origin refused (tested). Guest-room scoping and collaboration annotations are D03 and **untested**; the legacy `/api/v1` surface has no repo/host ACL (audit). |
| Malicious fixture | **Pass** for symlinks (file and dir), config-hook/fsmonitor execution, path escapes, argument injection, oversize files, timeout + grandchild kill, cancellation, memory limit, env scrubbing, output cap. Prompt injection and secret redaction: not applicable to a no-LLM static extractor / not tested. |
| UI states and a11y | **Mostly pass**: empty, disabled, partial, stale, failed, running, keyboard, table semantics, themes, layouts. Loading and API-error states are not covered by a test. |
| Benchmarks | **Partial**: measured and reported; noisy host; no incremental |
| CI green, reversible migrations, flag off, pinned upstream/notices | Flag off + pinned/notices **done**; no migration to reverse; **CI (Woodpecker) not yet run**. |

**Do not read this as "feature complete".** It is a working, tested vertical slice for Python repositories.

## Known risks / next work
1. E01 incremental (vendor `fingerprint_diff.py`, parity fixtures); D02/D03 correlation and collaboration; lease queue + UI "request analysis".
2. Optional LSP `calls` edges (pyright in the worker image; replace upstream pickle cache).
3. Language coverage beyond Python (tree-sitter JS/TS) — herdr's own UI code is invisible today.
4. Shared choke points `api.py`, `app.js`, `ingest.py` may conflict with sibling worktrees; edits there are minimal.
