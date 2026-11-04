# Build-Gate: cross-agent CPU build-storm prevention

Status: **implemented, deployed, live-verified** (2026-10-16/17).

## Problem

Autonomous pi sessions (and the workers/subagents they spawn) each fire
full-workspace builds (`cargo check --workspace --tests`, `tsc --noEmit`)
independently. With no shared gate, N agents launch N×`-j` compiler jobs at
once and CPU-starve the host. Observed on bender: 24 cores, load 27→46, the
dev-fabric gateway and the machine became unresponsive.

## Solution (two pieces)

1. **`herdr-eng build-coordinate`** — a per-host, flock-based slot gate
   (`herdr_engineering/build_coord.py`). Acquires one of N slot locks, then
   `execvp`s the build so the flock is held for the child's full lifetime and
   released automatically on exit. Load-aware backoff refuses new builds while
   1-min load/core exceeds `--max-load`. `--bypass-on-timeout` runs the build
   **ungated** (never fails/hangs the agent) if no slot frees within `--wait`,
   making the gate best-effort coordination.

   Defaults: `slots` = **auto** (`min(4, cpu_cores-4)`, floor 2), `max-load=3.0`,
   `wait=900s`, `bypass-on-timeout`. The auto slot count scales with the
   host: 2 on small hosts, capped at 4 on large hosts (e.g. **4 on 24
   cores**). Override with `--slots` / `HERDR_BUILD_GATE_SLOTS`.

2. **Pi build-gate extension** (`deploy/pi-build-gate/build-gate.ts`) — hooks
   pi's `tool_call` event and rewrites heavy build commands
   (`cargo check|build|test|clippy` + `--workspace|--all-targets|--tests|--all-features`,
   and any `tsc`) to route through the gate. Single-crate `-p foo` checks,
   `git status`, and compound commands are left ungated. Resolves `herdr-eng`
   to an absolute path at load time and **no-ops (builds pass through) if it is
   absent** — it can never break a build, only coordinate it.

## Deployment

- Extension is version-controlled at `deploy/pi-build-gate/build-gate.ts`
  (install via `deploy/pi-build-gate/install.sh`), and deployed to every fleet
  host by Ansible (`ansible/tasks/pi-build-gate.yml` + `site.yml`).
- `herdr-eng` (with `build-coordinate`) is provisioned fleet-wide by
  `ansible/tasks/fabric-agent.yml`.

Current fleet state (2026-10-17):

| host | pi | herdr-eng | build-gate ext | notes |
|------|----|-----------|----------------|-------|
| bender | ✅ | ✅ | ✅ active | live-verified |
| beast  | ✅ | ✅ | ✅ (next pi restart) | herdr-eng in `~/.venvs/herdr-engineering` |
| fry    | ✗ unreachable | ✗ | — | pending full `site.yml` |
| mac    | ✗ unreachable | ✗ | — | pending full `site.yml` |

## Verification

### Live pi e2e (bender)
A real `pi --print` session issuing `cargo check --workspace --tests` was
intercepted and routed through the gate. Slot payload confirmed the build ran
under `herdr-eng build-coordinate` (host bender, slot 0).

### Multi-agent contention (bender, 2026-10-17)
4 concurrent gated builds, `slots=2`, each `sleep 6s`:

- **max concurrent builds: 2** (exactly the configured bound)

The automatic default is intentionally conservative: two slots on small hosts
and at most four on larger hosts. Full-workspace compilers parallelize inside a
slot, so multiplying slots by CPU count creates a second-level build storm for
agent-heavy interactive machines. Use `--slots` or `HERDR_BUILD_GATE_SLOTS` for
an explicit higher-throughput batch host.
- **wall time: 12.1s** = 2 batches of 6s (builds 2,4 then 1,3) — perfect serialization
- 2 contention telemetry events recorded

Without the gate the same 4 builds would run fully parallel (~6s) and, at the
fleet scale of 14+ concurrent full-workspace cargo builds, starve the CPU.

## Monitoring & tuning

- `scripts/monitor-fabric-gateway.sh` reports gateway health/CPU, lease-store
  size + active/expired counts, host load/core, and build-gate slot/contention
  usage (`--json`, `--alerts`).
- `scripts/herdr-gateway-monitor.{service,timer}`: systemd user timer (every
  5m) appending JSON to `~/.local/share/herdr-engineering/gateway-monitor.log`.
- The gate appends a `contention` line to
  `state/build-gates/telemetry.jsonl` whenever a build has to wait (ts/slots/
  max_load/load_core/busy_pids), so `--slots`/`--max-load` can be tuned from
  real data.

**Tuning guidance** (revisit after a few days of real traffic):
- frequent contention **and** high load/core → gate too loose; raise load
  guard or lower slots (e.g. pin `HERDR_BUILD_GATE_SLOTS=2`).
- frequent contention **but** idle host → gate too conservative; raise slots.
- Contention counts: `grep -c '"event": *"contention"' state/build-gates/telemetry.jsonl`

> **Note on slots vs the load guard.** The auto default is
> `min(4, cores-4)`, floor 2 (4 on 24 cores). If every build is a full-
> workspace `cargo check` using `-j nproc`, even 4 concurrent builds can
> saturate the CPU — the `--max-load` guard only stops *new* builds from
> starting once load/core is high; it does not stop already-running ones. If
> you see contention + sustained high load after deployment, pin
> `HERDR_BUILD_GATE_SLOTS` to a lower value and/or lower `HERDR_BUILD_GATE_MAX_LOAD`.

## Recording build metrics (opt-in)

`herdr-eng build-coordinate --record -- <cmd...>` forks the child instead of
`exec`ing it, so the wrapper stays alive holding the slot and the maintenance
`LOCK_SH` until the child is reaped (cleanup still cannot run mid-build). It
forwards SIGINT/SIGTERM to the child only and returns the child's exit code
(signal death = 128+sig). Without `--record` behavior is unchanged (`exec`).

Two JSON lines per build are appended (0600, rotated to `builds.jsonl.1` above
5 MiB) to `<state>/build-gates/builds.jsonl`: `build_started` and
`build_finished` with `build_id`, `host`, `slot`, `cmd_kind` (allowlist only:
`cargo build|test|check|clippy`, `tsc`, `other`; argv and env are never
recorded), `exit_code`, `elapsed_s`, `user_cpu_s`, `sys_cpu_s`, `max_rss_kb`,
`rusage_oublock`, `proc_io_write_bytes` (logical, sampled from `/proc/<pid>/io`
over the child tree; a lower bound), `host_disk_write_bytes_delta` with
`host_disk_scope` (`device:<name>` or `host_wide_physical_ish`; includes other
activity on the host), `sccache_stats` (delta of numeric counters from
`sccache --show-stats --stats-format json`, 2 s timeout), and `unavailable`
listing every field that could not be measured (those fields are `null`, never
zero). Measurement errors never change the build result. Note `sccache
--show-stats` may start the sccache server if it is not running.
`build-coordinate --status` includes a summary (counts, p50/p95 by `cmd_kind`,
total writes). `scripts/build_bench.py` runs a cold/warm benchmark outside the gate.

Rust cache maintenance uses the same per-host state directory and a shared build
lock. See [automatic Rust build-cache cleanup](build-cleanup.md) for configuration,
process checks, and the optional Herdr server companion.

## Checking it

The `build-gate` skill (`skills/build-gate/`) wraps this check: `bash skills/build-gate/scripts/status.sh` reports whether `herdr-eng` is on PATH, whether the extension is installed, live slot occupancy and host load, and exits nonzero when gating is not active.
