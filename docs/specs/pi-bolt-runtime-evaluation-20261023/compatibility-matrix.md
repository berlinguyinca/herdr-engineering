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

## Real extension set (pi-engineering release ef5130f2 + user extensions build-gate.ts, herdr-agent-state.ts)
Same package set under every arm, isolated dirs, no credentials, `PI_OFFLINE=1`, probe records commands/tools after load.
Raw rows: `results/compat-extload.jsonl`. Host load average 90-120 during all runs (noisy; arms interleaved per repetition).

| Result | standard-pi 1.4.0 | pi-bolt | pi-bolt-jit |
|---|---|---|---|
| Loads, no host fallback | yes (3/3) | yes (3/3) | yes (3/3; 1 of 2 later cost runs hit the 120 s watchdog) |
| Registered commands / tools | 44 / 17 | 45 / 17 (+`usage`) | 45 / 17 (+`usage`) |
| Missing commands/tools vs standard | - | none | none |
| Time to ready, reps 1-3 (s) | 11.0, 19.3, 10.6 | 48.8, 95.8, 80.9 | 57.1, 89.1, 61.9 |
| CPU seconds / peak RSS (2 cost reps) | 1.9 s / 141 MiB | 24.4 s / 384 MiB, 24.2 s / 369 MiB | 24.9 s / 370 MiB (rep 2 killed at 120 s) |

Reading: functionally equivalent for this set, but with all extensions loaded Pi-Bolt (interpreted run-time loading through jiti) needed
roughly 5-8x the time to ready, ~13x the CPU and ~2.6x the peak RSS of standard Pi. This inverts the extension-free startup result
(`--version`: 94 ms vs 1432 ms). Pi-Bolt's own docs say run-time loaded plugins on the JIT-off build are transpiled by jiti at every
start and run in the interpreter, and recommend compiling plugins in. The JIT build did not fix load time.

## Bundled mode (`pi-bolt compile`) — NOT achieved
Kit and runtime for linux-x64 were fetched and checked against the signed SHA256SUMS. Attempts on the engineering release:
1. Entry files via the `extensions` setting: build stops, `../../../extensions/index.ts` unresolved (only the entry's folder is copied).
2. Whole package under `<agent>/extensions/`: build stops on `import("pi-blackhole")` (BlackholeAdapter.ts:68), an optional dependency that is
   not installed and is guarded at run time; the bundler requires every dynamic specifier to resolve.
3. With an empty stub for `pi-blackhole` in a scratch copy: the compiler exits after ~40 s with `error: the build stopped:` and no diagnostic
   (its build.log is deleted on exit). Unresolved; would need Pi-Bolt maintainers or a source checkout with `scripts/build-pi.sh`.
So the one mode that could remove the extension-load penalty is blocked for pi-engineering today.

Not yet tested (no claim): tool execution, session restore, OpenViking/memory extensions (need their services/config), model calls,
Herdr pane launch, Apple silicon macOS host, beast, Docker/Apptainer, musl. `-p hi` with no model did not exit within 60s on ANY arm
(same behavior; not a Pi-Bolt difference).

Incompatibilities / risks logged so far
1. Pi-Bolt adds a `usage` command: a name collision risk with any extension registering `usage`. None observed.
2. `process.versions.node` is a shim value under Bun (26.3.0): any extension gating on node version sees a different value.
3. Managed launcher wrapper (`scripts/install-session-auto-resume.ts`) runs under node and imports Pi's package; it must stay in front of a compiled binary.
4. macOS: Apple silicon only; Intel Macs and musl hosts must stay on standard Pi.
5. Extension load cost under Pi-Bolt (see above): 5-8x slower to ready, ~13x CPU, ~2.6x RSS with the real extension set.
6. `pi-bolt compile` cannot bundle the pi-engineering package today (relative imports outside the entry folder; optional `pi-blackhole` import).
7. Pi-Bolt's compiled binary replaces `.bin/pi` only behind the node launcher; extension code that touches Pi internals via
   `@earendil-works/pi-coding-agent` imports resolves to the binary's bundled copy, not the release's node_modules copy.
