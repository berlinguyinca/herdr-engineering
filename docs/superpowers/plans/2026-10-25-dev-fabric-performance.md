# Dev Fabric and HerdR Engineering Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dev fabric and HerdR Engineering runtime scale predictably as agent, service, reporter, and feature counts increase.

**Architecture:** Apply bounded concurrency and observability at the control-plane boundary first, then reduce duplicate observation with cursors/single-flight collection, optimize fabric discovery, constrain build fan-out, and finally make deployment provenance explicit. Preserve public contracts and keep live deployment separate from the source changes.

**Tech Stack:** Python stdlib HTTP servers, asyncio-compatible control-plane stores, JSON/HTTP event contracts, pytest, shell/systemd/docker deployment templates.

**Spec:** `docs/superpowers/specs/2026-10-25-dev-fabric-performance-design.md`

## Global Constraints

- Preserve API, lease, auth, and private-listener contracts.
- Add no third-party dependencies.
- Write and observe failing tests before production changes.
- Do not restart live services or rotate credentials automatically.

### Task 1: Bound control-plane request concurrency

**Files:**
- Modify: `herdr_engineering/control_plane/api.py`
- Test: `tests/test_control_plane_api.py` or the repository's existing API test module
- Modify: `herdr_engineering/config.py` and the example config if a setting is needed

**Interfaces:**
- Add a configurable request worker limit with a safe default.
- Preserve existing routes and status payloads.

- [ ] Add a regression test that starts the API with a small worker limit and proves excess requests do not create unbounded concurrent handlers.
- [ ] Run the focused test and observe the expected failure.
- [ ] Implement bounded request dispatch using existing stdlib primitives.
- [ ] Add route/ingest counters and bounded latency measurements to diagnostics.
- [ ] Run focused API tests and existing control-plane tests.

### Task 2: Batch and coalesce ingestion

**Files:**
- Modify: `herdr_engineering/control_plane/ingest.py`
- Modify: `herdr_engineering/control_plane/api.py`
- Modify: reporter modules that already emit ingest envelopes
- Test: existing ingest/feed test modules

**Interfaces:**
- Support a bounded batch of existing envelopes without changing single-envelope behavior.
- Preserve idempotency and validation errors.

- [ ] Add failing tests for batch acceptance, bounded batch size, and duplicate heartbeat coalescing.
- [ ] Implement minimal batch handling and one persistence boundary per batch.
- [ ] Run focused ingest tests.

### Task 3: Add single-flight/cursor collection

**Files:**
- Modify: `herdr_engineering/mission_activity_feed.py`
- Modify: `deploy/herdr-cp-feed-plugin/snapshot.js`
- Modify: `herdr_engineering/service_metadata_feed.py` if compatible
- Test: feed and snapshot tests

**Interfaces:**
- Reporters retain a cursor/last-seen revision and skip unchanged payloads.
- A pass cannot overlap itself.

- [ ] Add failing tests for unchanged snapshots, changed snapshots, and overlapping pass suppression.
- [ ] Implement cursor and single-flight guards with existing state mechanisms.
- [ ] Run feed tests and a local request-count smoke test.

### Task 4: Make fabric discovery incremental

**Files:**
- Modify: `herdr_engineering/fabric_agent.py`
- Modify: `herdr_engineering/fabric.py`
- Test: fabric-agent and gateway tests
- Modify: `config/herdr-engineering.example.yaml` and docs where cadence is documented

**Interfaces:**
- `scan_once()` retains behavior for new/removed listeners while avoiding repeated HTTP probes for unchanged listeners.
- Gateway probes and metadata lookups are bounded/cached.

- [ ] Add failing tests proving unchanged listeners are not re-probed and gateway metadata is not fetched repeatedly in one cycle.
- [ ] Implement the smallest cache/change-detection layer.
- [ ] Run focused fabric tests and proxy/lease regression tests.

### Task 5: Conservative build-gate defaults and telemetry

**Files:**
- Modify: `herdr_engineering/build_coord.py`
- Modify: `deploy/pi-build-gate/build-gate.ts`
- Modify: `docs/build-gate.md` and example configuration
- Test: build-gate tests

**Interfaces:**
- Default slots are bounded conservatively; explicit environment/CLI overrides remain supported.
- Status exposes configured/active slots and bypass count.

- [ ] Add failing tests for the default policy on 24-core and small hosts.
- [ ] Implement the conservative default and bypass telemetry.
- [ ] Run focused build-gate tests and status checks.

### Task 6: Deployment provenance and convergence tooling

**Files:**
- Modify: `herdr_engineering/version.py`, CLI health/fingerprint paths, and fleet scripts
- Modify: deployment docs/service templates
- Test: version/fingerprint/fleet-status tests

**Interfaces:**
- Runtime fingerprint reports commit, source/release identifier, and installed binary path without secrets.

- [ ] Add failing tests for fingerprint fields and secret-free output.
- [ ] Implement provenance reporting and document the canonical release workflow.
- [ ] Run fleet/status tests and a dry-run deployment inspection.

### Task 7: Full verification and performance evidence

**Files:**
- Modify: only tests/docs if verification reveals a defect

- [ ] Run the focused suites for all six tasks.
- [ ] Run the full Python test suite, lint, and type/static checks available in the repo.
- [ ] Run a local smoke benchmark comparing request concurrency, feed request count, fabric probe count, and build-gate defaults.
- [ ] Review the diff for scope, secrets, and live-runtime mutations.
- [ ] Report changed files, measured improvements, remaining risks, and deployment steps that require a separate operator rollout.
