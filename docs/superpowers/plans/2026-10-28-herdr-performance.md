# HerdR Performance Implementation Plan

> **For agentic workers:** Use bounded native subagents for disjoint implementation slices; the leader integrates and verifies each slice.

**Goal:** Serialize production releases, accelerate complete-state restart, and page/filter service catalogs without losing history or UX.

**Architecture:** OS-level deployment ownership spans forward and rollback. Explicit JSON projection checkpoints use a commit-fenced durable frontier and retain database-backed history. Catalog pages carry exact global metadata and preserve the existing full-list API.

**Tech Stack:** Python, PostgreSQL/asyncpg, Bash/flock, vanilla JavaScript, existing pytest/Playwright.

**Spec:** `docs/superpowers/specs/2026-10-28-herdr-performance-design.md`

## Global Constraints

- Preserve all retained raw events, identities, privacy boundaries, and legacy API shapes.
- No new dependencies or pickle; no temporary HOME/configuration deployment tricks.
- No service mutation until concurrent ownership and rollback checks pass.
- Keep catalog counts/facets independent of page size and unrelated filters.
- Publish only verified checkpoints; unsafe recovery falls back explicitly.

## Task 1: Coordinated deployment ownership

**Files:** `deploy/control-plane/deploy.sh`, `deploy-state.py`, new `deploy-lock.sh`, `tests/test_deploy_scripts.py`, new `tests/test_deploy_coordination.py`.

**Interfaces:** lock acquisition establishes an inherited/open descriptor, owner token and owner metadata. State helper actions create/update/clear only the current owner's metadata. Existing deployment arguments and receipts remain compatible.

- [ ] Add subprocess tests holding the first rollout at build/verification and prove a second checkout does not reach Docker or host mutation.
- [ ] Test a real outer `flock` wrapper, stale metadata, concurrent dry-run, ordinary failure, and signal-triggered protected rollback.
- [ ] Implement inode-verified inherited descriptor adoption or an owned descriptor; nonblocking contention exits 75 with owner details.
- [ ] Keep ownership until rollback/cleanup completes, and share one rollback function for failed verification and interruption.
- [ ] Test the stable launcher/legacy-shim installer against disposable paths; install only after validation and backup known entry points.
- [ ] Run `pytest -q tests/test_deploy_coordination.py tests/test_deploy_scripts.py tests/test_compose_render.py`, Bash syntax, and scoped Ruff; commit with validation evidence.

## Task 2: Explicit projection state and durable replay frontier

**Files:** new `control_plane/projection_checkpoint.py`, domain checkpoint codecs in `envelope.py`, `inspector.py`, `host_metrics_store.py`, `mission_activity.py`; `repo.py`, `bridge.py`, `events.py`; an additive SQL migration; dedicated checkpoint/history tests.

**Interfaces:** versioned `export_checkpoint()`/`restore_checkpoint()` methods contain JSON-only state. A checkpoint source captures a fenced repeatable-read snapshot, loads a validated checkpoint, reads changes after its frontier, reconstructs dirty streams, and atomically publishes verified state. Raw-history provider implements `stream`, `stream_page`, `entity_ids`, and bounded recent reads with privacy preserved at callers.

- [ ] Lock down equivalence fixtures containing core, delivery, usage, stage, agent, inspector, graph, collaboration/quarantine, host metrics, ledger gaps, and sequence collisions.
- [ ] Add whitelisted domain codecs that retain callback/clock bindings but recreate locks/subscribers/process epochs; reject malformed/version-incompatible input.
- [ ] Add server-owned positions and writer fences without rewriting/deleting legacy raw rows; test late lower-ID commits on real Postgres.
- [ ] Implement complete checkpoint envelope/checksum/compatibility and atomic last-valid publication.
- [ ] Restore compact projections, refresh structured overlays, and apply/reconstruct every affected tail stream without duplicating side effects.
- [ ] Attach database-backed complete history, bounded recent activity/SSE, and stable cursor traversal across restart.
- [ ] Integrate checkpoint preparation/refresh into startup and deployment while the previous release is available; expose safe recovery-mode/timing diagnostics.
- [ ] Run full-versus-fast equivalence and real Postgres failure/race/retention tests, then measure a production-sized read-only candidate before rollout.

## Task 3: Catalog query snapshots and paged UI

**Files:** new `control_plane/service_catalog_query.py`, existing `service_catalog.py`/`project_links.py`, `repo.py` wrapper and `api.py` dispatch; `client.js`, `services-view.js`, `app.js`; dedicated backend/client/browser tests.

**Interfaces:** `service_catalog_page(scope, status, include_archived, search, host_id, group_by, limit, cursor)` returns page rows and exact counts/facets/group metadata. `listServiceCatalog(options)` opts in; `listServices()` remains unchanged.

- [ ] Mirror and cross-test display-state/search/scope policy, deterministic Unicode ordering, and archive semantics.
- [ ] Add bounded snapshot cursors, filter binding, stale restart errors, exact global metadata, and one project-map read per snapshot.
- [ ] Dispatch catalog requests before legacy full projection and validate all query inputs.
- [ ] Add paged mounting with append/group merging, accurate totals, generation cancellation, failure retention and local expiry; fetch one service for detail refresh.
- [ ] Verify legacy callers, large-catalog latency/payload, all filters, group-open state, keyboard focus and mobile containment.

## Task 4: Integrated release

- [ ] Review cross-lane diffs, preserve newer upstream work, and run applicable backend/client/browser/UI gates.
- [ ] Verify recovery equivalence and catalog output against the real retained dataset without production writes.
- [ ] Commit/publish the final source, prepare a compatible checkpoint while the prior release serves, and deploy through the coordinated entry point.
- [ ] Confirm deployed source/config, stable services, raw-history preservation, exact catalog counts, restored naming, and measured startup/latency improvements.
- [ ] Archive evidence, remove only owned disposable resources, and report remaining limits honestly.
