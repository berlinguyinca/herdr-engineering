# Compatibility matrix (in progress) — 2026-10-23

Host: bender (linux-x64, glibc 2.39, AVX2). Pi-Bolt bolt-v1.0.0 (Pi 1.4.0); baseline Pi 1.4.0 on node 22.23.3.
Method: `bench/probe/` extension records the host-fallback flag and `pi.getCommands()` after load; isolated
PI_CODING_AGENT_DIR and PI_ENGINEERING_STATE_DIR, `PI_OFFLINE=1`, no credentials, `-p hi --no-session`.
pi-engineering = origin/main 96dfd2b (worktree, `npm ci --ignore-scripts`).

| Check | standard-pi 1.4.0 | pi-bolt | pi-bolt-jit |
|---|---|---|---|
| Starts / `--version` | pass | pass (1.4.0, Pi-Bolt 1.0.0) | pass |
| pi-engineering extension loads (entry shim, no host fallback) | pass | pass | pass |
| Registered commands | 45 | 46 (+`usage`, Pi-Bolt's own) | 46 (+`usage`) |
| Runtime reported by process | node 22.23.3 | Bun, reports node 26.3.0 | Bun, reports node 26.3.0 |

Not yet tested (no claim): tool execution, session restore, OpenViking/memory extensions (need their services/config), model calls,
Herdr pane launch, Apple silicon macOS host, beast, Docker/Apptainer, musl. `-p hi` with no model did not exit within 60s on ANY arm
(same behavior; not a Pi-Bolt difference).

Incompatibilities / risks logged so far
1. Pi-Bolt adds a `usage` command: a name collision risk with any extension registering `usage`. None observed.
2. `process.versions.node` is a shim value under Bun (26.3.0): any extension gating on node version sees a different value.
3. Managed launcher wrapper (`scripts/install-session-auto-resume.ts`) runs under node and imports Pi's package; it must stay in front of a compiled binary.
4. macOS: Apple silicon only; Intel Macs and musl hosts must stay on standard Pi.
