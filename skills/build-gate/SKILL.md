---
name: build-gate
description: Check and explain the per-host build gate that throttles heavy cargo/tsc builds across pi sessions (herdr-eng build-coordinate). Use when asked whether build gating is active, how many build slots are busy or free, why a cargo/tsc build is waiting or slow, or whether the build-gate pi extension loaded on this machine. Triggers on "build gate", "build slots", "build-coordinate", "build storm", "BUILD GATING DISABLED".
---

# Build gate

Heavy `cargo check|build|test|clippy` (with `--workspace`, `--all-targets`, `--tests`,
`--all-features`) and any `tsc` run by a pi session are rewritten by the `build-gate` pi
extension (`~/.pi/agent/extensions/build-gate.ts`) to go through
`herdr-eng build-coordinate`, which hands out a bounded number of host-wide slots.
Single-crate `-p` checks are not gated.

## Check it

Run the bundled script; it reports every link in the chain and ends with a verdict:

```bash
bash ~/.agents/skills/build-gate/scripts/status.sh
```

It checks that `herdr-eng` resolves on PATH (the extension needs it at load time, and
without it gating is silently off), that the extension file is installed, the live slot
occupancy (`herdr-eng build-coordinate --status`), host load against the back-off
threshold, and any `HERDR_BUILD_GATE_*` overrides.

## Tuning (env vars, set before starting pi)

- `HERDR_BUILD_GATE_DISABLE=1` off
- `HERDR_BUILD_GATE_SLOTS=N` max concurrent builds (default max(2, cores-4))
- `HERDR_BUILD_GATE_MAX_LOAD=F` back off above this 1-min load per core (default 3.0)
- `HERDR_BUILD_GATE_WAIT=S` seconds to wait for a slot before running ungated (default 900)

## If it says DISABLED

Install `herdr-eng` (`uv tool install --python 3.12 -e .` in the herdr-engineering repo),
make sure `~/.local/bin` is on PATH for the shell that launches pi, then restart pi: the
binary is looked up once, at extension load.
