# Herdr Fabric Resource Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one policy-safe, telemetry-aware Herdr agent placement service and integrate it with current control-plane, collaboration, native Herdr, and dashboard seams.

**Architecture:** Keep `ControlPlaneRepo` and its event/SSE model authoritative. Add small pure domain modules for identity/policy, telemetry normalization, admission/scoring, workspace readiness, lifecycle/recovery, and replay; expose them through existing API/auth paths and use native Herdr adapters for actual panes/workspaces. Mac defaults to manual-only, InferWeave keeps inference/GPU worker ownership, and migration stays disabled until fencing/checkpoint contracts are proven.

**Tech Stack:** Python 3.12, pytest, existing stdlib HTTP/control-plane code, PostgreSQL migrations, ES modules/Node tests/Playwright for the existing dashboard.

**Spec:** `docs/specs/herdr-fabric-scheduler-2026-10-25-20261025230542-2147563/bundle/herdr-fabric-scheduler-2026-10-25/README.md` and ordered phase files P00–P10.

## Global Constraints

- Mac is `manual_only` by default and never an implicit parent/child target.
- Unknown, stale, untrusted, unreachable, disabled, draining, or conflicting hosts are not auto-eligible.
- One admission/placement interface serves every launch adapter; no second orchestrator.
- Reservations are durable, idempotent by `request_id`, atomically rechecked at bind, and lease-based.
- Never claim migration safety without checkpoint capability and single-writer fencing; keep `scheduler.migration` off.
- InferWeave owns inference/model/GPU worker scheduling.
- Do not expose credentials, command arguments, private topology, or checkpoints through public routes/artifacts.
- Preserve unrelated dirty work and existing behavior via focused tests and feature flags.

### Task 1: P01 host identity and policy

Files: create `herdr_engineering/host_policy.py`, tests; modify host records/ingest and add migration. Implement stable 0600 UID, alias normalization, clone conflict fail-closed, policy modes, Mac detection/default, generation/ETag and audit events. Start with red tests for alias bypass, Mac child inheritance, disabled/draining/stale/duplicate identity.

### Task 2: P02 telemetry and capacity

Files: create `herdr_engineering/telemetry.py`, tests; extend host ingestion/projection and migration. Normalize sequence/timestamps/confidence, EWMA/PSI/pressure/freshness, resource reservations, Linux/macOS differences, and source/error reporting without arguments/secrets.

### Task 3: P03 admission and placement

Files: create `herdr_engineering/scheduler.py`, tests; extend repo persistence/API and migration. Implement `plan`, `reserve`, `commit`, `release`, `cancel`, deterministic explainable score, fair near-tie rotation, queue reasons, idempotency, TTL, policy-generation recheck, and bounded concurrency.

### Task 4: P04 adapters

Files: add adapter registry and contract tests; modify `collab-spawn.py`, native integration seams, and API launch handlers. Route all audited creation paths through placement, preserving native pane/workspace/room/lineage semantics and cross-host child placement.

### Task 5: P05 workspace readiness

Files: create focused workspace preparation/manifest module and tests; reuse `NativeHerdrAdapter`, RustFS, and existing artifact/session stores. Validate repo/revision/tool/access/disk, isolated worktrees, checksums, dirty-state handling, and resumable metadata.

### Task 6: P06 quotas and lifecycle

Files: create lifecycle/fair-queue module/tests; extend scheduler persistence. Add profiles/envelopes, per-host/owner/project/mission/workspace limits, aging, interactive priority, recursive fanout caps, lifecycle states, drain/hibernation semantics, and non-Herdr load accounting.

### Task 7: P07 dashboard

Files: extend existing client/API, Hosts/Fleet views, routes/styles, and JS/Playwright tests. Add scheduling overview, host policies/details, launch preview, explanations, queue/history/simulator read-only states, stale/accessible responsive UI, and optimistic policy mutation rollback.

### Task 8: P08 recovery/fencing

Files: create recovery/checkpoint/fencing module/tests and migration. Implement suspect/reconciling states, monotonic execution leases, single-writer tokens, safe migration plans, explicit unsupported-family blocking, and fault-matrix reconciliation; keep execution flag off.

### Task 9: P09 replay/advisory learning

Files: create sanitized history replay/estimator module/tests/API/UI panel. Produce counterfactual metrics and p50/p90 proposals only; never auto-relax safety/policy/ACLs.

### Task 10: P10 rollout and acceptance

Files: add flags/config, operational docs, rollout/rollback checklist, benchmark/fault/browser tests, and `verification.md`. Run project-native Python/Node/Playwright checks, verify 100 concurrent no-double-assignment and Mac/manual-only gates, and document unresolved unsupported paths honestly.

