# Dev Fabric and HerdR Engineering Performance Design

## Goal

Keep HerdR responsive as agent, service, reporter, and feature counts grow by bounding concurrency, centralizing repeated observation, reducing polling work, and converging the deployed runtime on one canonical release.

## Scope and order

The work is intentionally sequenced so each phase reduces pressure before the next phase adds more coordination:

1. Stabilize the control-plane web server and ingestion path.
2. Centralize and deduplicate reporter/polling work.
3. Make dev-fabric discovery incremental and bounded.
4. Lower and enforce build concurrency.
5. Make deployment provenance explicit and converge services on the canonical checkout.

## Design

### 1. Control-plane stabilization

Replace unbounded request-thread creation with a bounded request executor. Preserve the existing HTTP API and response semantics, but reject or queue excess work predictably rather than allowing hundreds of request threads to accumulate. Add lightweight route/ingest counters and latency histograms exposed through the existing health/diagnostic surface.

Ingestion remains idempotent, but reporters may submit batches. The server applies one batch under one persistence boundary and coalesces repeated heartbeat/snapshot updates before durable writes.

### 2. Observation and ingestion

Reporters use sequence/cursor state and submit only changed entities. A host-side collector owns native Herdr/session/service observations; the UI reads a cached projection. Existing endpoints remain available for compatibility, while new cache and cursor helpers are internal first.

Polling intervals remain configurable, but steady-state work is event- or change-driven where possible. Every loop has a single-flight guard so a slow pass cannot overlap the next pass.

### 3. Dev fabric

The agent caches the last listener snapshot and probes only new or changed candidate ports. Gateway health and front-door metadata are cached for the scan interval. Probe and reporting work use bounded concurrency and cannot delay lease heartbeats indefinitely.

The gateway continues to own leases and routing. This phase changes observation cadence and concurrency, not the lease contract.

### 4. Build gate

The default slot policy becomes conservative for interactive hosts and remains explicitly overrideable. Build-gate status reports configured slots, active slots, bypasses, and contention. Heavy build wrappers must use the gate; bypasses are observable and do not silently defeat the policy.

### 5. Deployment convergence

Health output and fleet fingerprints include the deployed HerdR Engineering commit and source path. The deployment documentation and service templates use one canonical release input. Runtime rollout is explicit and separate from code changes; this implementation does not restart or mutate live services automatically.

## Compatibility and safety

- No public listener or authentication boundary is weakened.
- Existing API routes and lease formats remain backward compatible.
- No new third-party dependencies.
- All new behavior is covered by focused regression tests before implementation changes.
- Live containers, user services, and credentials are not modified by this code change.

## Success criteria

- Request concurrency is bounded and observable.
- Repeated reporter work is coalesced or cursor-based.
- Fabric scans avoid repeated probes for unchanged listeners.
- Build defaults cannot launch 20 concurrent full-workspace builds on a 24-core host without an explicit override.
- A runtime fingerprint can prove which commit and checkout are deployed.
- Targeted tests, full unit tests, lint/type checks, and a smoke benchmark provide fresh evidence.
