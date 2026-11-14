# Portable measured-build waiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Recorded builds complete correctly on macOS Python 3.12 while preserving safe process ownership.

**Architecture:** Isolate non-reaping exit observation behind a typed helper. Keep signal shutdown, registry halt and resource collection in their existing order.

**Tech Stack:** Python 3.12 stdlib, pytest, real POSIX subprocesses, GitHub Actions macOS/Linux.

**Spec:** `docs/superpowers/specs/2026-11-13-macos-build-metrics-design.md`

## Global Constraints

- No new dependencies or mocks; isolated state; never signal unrelated processes.
- Preserve exit codes, CPU accounting, locks and cancellation; required review and green CI before normal merge.

## Review Focus

- Children that exit before registration must still be observable and reapable.
- Delayed children must not be reaped before registry cancellation stops.
- Signals during waiting must reach only the child.
- Queue descriptors must close on successful and interrupted waits.
- Linux wait behavior and the non-recorded exec path must remain intact.

### Task 1: Portable exit observation and acceptance

**Files:** `herdr_engineering/build_metrics.py`, `tests/test_build_metrics_wait.py`, `.github/workflows/build-metrics.yml`, `.coveragerc-build-metrics`.

**Interfaces:** `_wait_without_reaping(pid: int) -> None` observes an owned child, leaving status and rusage for `wait4`.

- [x] Add real-process tests: already-zombie and delayed children, repeated short-lived commands, registry/signal behavior, isolated CLI E2E.
- [x] Run new tests and existing exit/daemon regressions; record missing-helper / missing-waitid RED.
- [x] Implement the helper and replace only the existing waitid observation loop.
- [x] Run new tests plus build_metrics, build_coord and build_registry regressions on macOS; verify statement/branch/function coverage and typing.
- [x] Add macOS/Linux Python 3.12 CI for the same real-process tests and wheel build; commit.
- [ ] Obtain exact-head Opus review, fix findings, watch applicable CI, merge normally and install the reviewed runtime on Mac; verify CLI exit/metrics and clean owned scratch.
