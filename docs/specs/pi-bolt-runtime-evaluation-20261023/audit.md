# Phase 0 audit (partial) — 2026-10-23

Status: PB-001 partially done, PB-002 partially done. No code changed. Standard Pi remains the default.

## Execution context
- Host: bender (Linux 6.8, Node v22.23.3). Spec fetched via scp from `macbook-pro` (Tailscale name; `ssh mac` does not resolve),
  `~/Downloads/pi-bolt-herdr-runtime-evaluation-20261023.zip`, sha256 `2a73b414...df33a`. Only `SPEC.md` was extracted (PROMPT.txt is not part of the repo).
- Note: PROMPT.txt inside the ZIP says the ZIP is on *beast*; the task said the Mac. It was on the Mac. Beast was not checked.
- Target repo chosen: `berlinguyinca/herdr-engineering` (Herdr is the control plane per the spec), worktree
  `herdr-engineering-pibolt`, branch `feat/pi-bolt-runtime-evaluation` off origin/main 8c29a13. The Pi-side extension repo
  `berlinguyinca/pi-engineering` will likely need changes too; `pi-engineering-runtime` is an older dev checkout (571 behind).

## Installed versions (verified)
| Component | Version |
|---|---|
| pi | 1.3.0 (bender) |
| herdr | 0.9.1 |
| pi-bolt | not installed |

## Verified Herdr integration points that mention `pi`
- `deploy/control-plane/collab-spawn.py:64-75` and `packages/collab-scripts/join.sh:109-115`: hard-coded `pi -p ...` commands, `FAMILY_BIN`.
- `herdr_engineering/pi_recovery.py`, `fleet_rollout.py:719`, `control_plane/ci_debug.py:56`: identify agents by `agent == "pi"`.
- `herdr_engineering/agent_mine.py:20`: session dir `~/.pi/agent/sessions`.
- `deploy/deploy-fleet-agents.sh:80`: `PI_CODING_AGENT_DIR` / `~/.pi/agent`.
- No existing runtime-adapter boundary found yet (grep for RuntimeAdapter/pi_bin found none). TO VERIFY in pi-engineering `src/`, which owns mission launching.

## Upstream Pi-Bolt (from github.com/opensec-git/Pi-Bolt README, 2026-10-23; NOT verified by us)
- MIT fork of Pi, AOT-compiled to a native executable (Bun/JSC bytecode + machine code). Independent of Pi's authors.
- The default build has **no JIT**; "JIT" is a separate `x64-jit`/`arm64-jit` build. The spec's `pi-bolt-jit` maps to that.
- Linux x86-64/ARM64 glibc>=2.17 (no musl), macOS 13+ Apple silicon only (no Intel Mac), Windows x64.
- Extensions loaded at runtime are interpreted unless compiled in via `pi-bolt compile` (Linux/macOS).
- Installer is `curl | sh`; manual download with SHA256SUMS exists. The spec forbids untrusted auto-update, so use manual download + checksum + pin.
- Its benchmarks compare against Pi **1.4.0**; we run 1.3.0. Version skew must be resolved before any comparison. Releases list looked empty on the page, so the version to pin is unconfirmed.
- Vendor numbers (e.g. 45 ms vs 128 ms ready) are hypotheses, not results.

## Gap matrix (initial)
| Feature | Current | Pi-Bolt mechanism | Risk | Test | Decision |
|---|---|---|---|---|---|
| Launch | pi-engineering launcher + hard-coded `pi` in collab scripts | binary swap | medium | PB-004 | pending audit of pi-engineering |
| Extensions (OpenViking, memory, etc.) | loaded from package dirs | interpreted / `pi-bolt compile` | high | PB-006 | pending |
| Session restore | `pi --session <file>` | claimed identical | medium | PB-005 | pending |
| Hosts | bender, beast (Linux); Mac | Apple silicon only on macOS | low | PB-011 | pending |
| Supply chain | npm pi | signed release + bundled Bun/JSC (LGPL) | medium | PB-002 | pending |

## Blocked / needs decision
Running benchmarks requires installing a third-party native binary. Not done.
