# HerdR Live Mission Inspection Implementation Plan

> Use superpowers:subagent-driven-development for the bounded ownership lanes below.

**Goal:** Show genuine live Pi mission and worker activity in HerdR web while preserving terminal-only attention reporting.

**Architecture:** A native same-user collector reads Pi's existing local control protocol and revision-matched snapshots. It posts a bounded projection through a dedicated, capability-gated mission activity channel. The web reads this projection without mutating mission records or generating ordinary events, attention, or global SSE notifications.

**Tech Stack:** Existing Python control plane and native CLI, plain JavaScript web components, existing HTTP/Unix sockets, pytest and Playwright. No new dependencies.

**Spec:** User request in this conversation and existing mission snapshot/agent inspector contracts.

## Constraints

- Keep source namespaces distinct: `pi:MSN-...` never silently attaches to a bare HerdR mission ID.
- Accept owned mission IDs only from a fresh Pi runtime status reply; snapshot identity and revision must match.
- Telemetry is observational data, not authorization for controls or proof of worker progress.
- Preserve worker output, heartbeat, source snapshot, collector observation and server receipt timestamps as separate facts.
- Bound and redact payloads before transport and at ingestion; exclude reasoning and arbitrary arguments.
- Stale/disconnected collection is not itself a mission crash or stall.
- Enable the collector only after the upgraded server advertises `mission_activity_v1`; the legacy server emits unknown entity types into its ordinary event feed.
- Preserve active Pi sessions and existing control-plane data during rollout.

## Backend lane

Owner: mission_activity_backend. Files: control_plane/mission_activity.py, repo.py, ingest.py, api.py and dedicated tests.

- [x] Add bounded projection storage and strict namespaced ingestion with no ordinary event/sink/attention side effects.
- [x] Add bulk GET `/api/v1/mission-activity` and detail GET `/api/v1/missions/<mission_key>/activity` with freshness and server time.
- [x] Advertise capability only when the route and ingestion handler are available.
- [x] Test bounds, redaction, copies, stale/disconnected state, source identity and ordering.
- [x] Fix sparse durable-event sequence lookup in `recent_events` with a regression before restored data is served.

## Collector lane

Owner: mission_activity_collector. Files: mission_activity_feed.py, minimal CLI wiring and dedicated tests.

- [x] Read protected same-user Pi descriptors and nonce-bound status replies with bounded Unix-socket requests.
- [x] Read bounded snapshots only for runtime-owned mission IDs and matching revisions.
- [x] Normalize a safe display projection; preserve missing/unavailable information explicitly.
- [x] Gate every publication capability, support once/interval operation and graceful shutdown, retain disconnected evidence without declaring mission failure.
- [x] Test real Unix-socket/HTTP boundaries, invalid ownership/revision, source timestamps and absence of notification writes.

## Frontend lane

Owner: mission_activity_frontend. Files: app.js, web client, mission view/components, CSS and browser tests.

- [x] Add a live Pi section above historical mission records; preserve explicit namespaces.
- [x] Add mission/worker detail with activity, route, waits, measured progress, output previews, tests, changes, recent activity and blockers.
- [x] Poll read-only endpoints every five seconds with single-flight/cancellation and update age labels locally every second.
- [x] Retain last known details with explicit stale/disconnected state; clean timers on navigation and hidden views.
- [x] Test real browser updates, route races, escaping, freshness, and zero mutating requests.

## Operational recovery and rollout

Owner: web_backend_rollout_audit with leader review.

- [x] Back up current API entity snapshots and all accessible raw event streams before replacing the existing web process.
- [x] Recover the stopped existing PostgreSQL container without replacing volumes.
- [x] Compare exported and durable event identities/sequences; review an idempotent backfill plan before writes.
- [x] Verify restored current state and replay parity before web-only image rollout.
- [ ] Build a uniquely tagged image from the final published revision; recreate only the control-plane web service with the same environment/network/volumes.
- [ ] Start the native collector only after capability/readiness checks, verify real Pi data and notification isolation in the deployed browser.

## Leader integration

- [x] Run affected Python/JavaScript/browser checks and independent review, resolve material findings.
- [ ] Merge through a green PR and record deployment provenance, backups, and rollback instructions.
- [ ] Confirm current missions/sessions survived, real worker telemetry is visible, stale signals are honest, and routine collection produces no attention events.

## Findings and rulings

- Existing PostgreSQL was stopped since Oct16 after a disk-full error; current live web memory must be secured before restart. Disk space is now available; starting the existing database is independent of replacing the web process.
- The old `/activity` endpoint fails on large reads because it confuses sparse event sequences with array indexes. Backups enumerate raw entity streams, and the backend lane repairs that lookup before rollout.
- Keep the collector independent from Pi runtime upgrades so already-running compatible sessions can be inspected without reload.

- Verification: full Python suite 753 passed, 4 skipped, 1 expected failure; JavaScript/Playwright 75 passed; independent review approved. Latest focused backend/collector regression suite: 345 passed, 3 skipped, 1 expected failure. Ruff and compilation are clean.
