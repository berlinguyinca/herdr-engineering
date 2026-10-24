# Audit: verified code locations and reuse decisions (2026-10-23)

Standard Pi remains the default; no production change was made. Verdict and data: `decision.md`.

## Execution context
- Host bender (Linux 6.8, node 22.23.3, glibc 2.39, AVX2). Spec fetched via scp from the Mac (`macbook-pro` on Tailscale; `ssh mac` does not resolve),
  `~/Downloads/pi-bolt-herdr-runtime-evaluation-20261023.zip` sha256 `2a73b414...df33a`; only `SPEC.md` was extracted.
  (`PROMPT.txt` inside the ZIP says the ZIP is on beast; it was on the Mac, beast was not checked.)
- Repos: `herdr-engineering` (spec, benchmarks; worktree `herdr-engineering-pibolt`, branch `feat/pi-bolt-runtime-evaluation`) and
  `pi-engineering` (adapter; worktree `pi-engineering-pibolt`, branch `feat/pi-bolt-runtime-adapter`). `pi-engineering-runtime` is an older
  dev checkout (571 commits behind) and was not used.
- Installed: pi 1.3.0 (fleet, unchanged), herdr 0.9.1. Evaluated: Pi 1.4.0 (private prefix) and Pi-Bolt bolt-v1.0.0 (Pi 1.4.0 base). Fleet-vs-eval version skew is deliberate.

## Verified integration points
| Area | Location (repo) | Finding | Decision |
|---|---|---|---|
| Orchestration seam | pi-engineering `src/runtime/AgentRuntime.ts`, `LegacyAgentRuntime.ts`, `herdr/HerdrAgentRuntime.ts` | `RuntimeName = "legacy" \| "herdr"`; an orchestration backend, not a Pi-binary choice. HerdrAgentRuntime asks Herdr to start agent kind `"pi"` | REUSE its contract tests; do NOT add a runtime type here |
| Pi launcher | pi-engineering `scripts/install-session-auto-resume.ts`, `scripts/pi-session-auto-resume.ts`, `src/gateway/launcherLinks.ts` | Managed wrapper `~/.pi/agent/bin/pi` execs the release `.bin/pi`; the hook runs a node launcher that imports the Pi CLI in-process | EXTENDED: runtime selection added to this launcher (commit 890fbd0); no second launcher |
| Config layering | pi-engineering `src/lifecycle/policy.ts` (`loadPolicy`) | Global < workspace YAML | REUSED the layering order; did not import `loadPolicy` into the launcher (it pulls the policy graph and made 5 launcher-install tests fail); small two-layer loader needing only `yaml` |
| Runtime events/telemetry | pi-engineering `src/runtime/isolation/runtimeEvents.ts`, `RuntimeSession.ts`, `src/telemetry/sink.ts` | `runtime.started` / `session.heartbeat` go to `runtime.jsonl` and the telemetry sink | REUSED: both events now carry runtime kind/source/engine/CPU/RSS (commit 271c0f6) |
| Control-plane ingest | pi-engineering `src/runtime/herdr/ControlPlaneIngest.ts`; herdr-engineering `herdr_engineering/control_plane/ingest.py` | Orchestrator emits worker `session.*` events but does not know which binary a worker's launcher chose | GAP: the dashboard view (PB-012) needs the worker to report `runtimeFacts()` to ingest; not implemented, see issue #191 |
| Worker identity for role/mission overrides | none | `PI_ENGINEERING_ROLE` / `PI_ENGINEERING_MISSION_ID` are not exported by the spawner today | GAP: config supports it, spawner change not made (#183/#187) |
| Herdr-side `pi` launches | herdr-engineering `deploy/control-plane/collab-spawn.py:64-75`, `packages/collab-scripts/join.sh:109-115` | Hard-coded `pi -p …` for collaboration agents (non-engineering) | Unchanged; they use whatever `pi` is on PATH, i.e. standard Pi |
| Pi agent detection / recovery | herdr-engineering `pi_recovery.py`, `fleet_rollout.py:719`, `control_plane/ci_debug.py:56` | Match on `agent == "pi"` | Unaffected by binary choice |
| Sessions | pi session JSONL under `~/.pi/agent/sessions` (`agent_mine.py:20`) | Same format both runtimes | Verified by round-trip |

## Upstream Pi-Bolt (primary source: opensec-git/Pi-Bolt release bolt-v1.0.0 and its README/docs)
- MIT; independent of Pi's authors; bundles Bun, JavaScriptCore (LGPL-2.0/BSD), ICU. AOT-compiled; the JIT build is a separate artifact.
- Verified here: SHA256SUMS signature (OpenSSL, Ed25519), tarball and kit/runtime checksums. Key obtained from the same repo (trust on first use).
- Platforms: Linux x64 (AVX2) / arm64 glibc, macOS Apple silicon, Windows x64; no musl, no Intel macOS.
- Extensions: run-time loaded via jiti and interpreted on the default build; `pi-bolt compile` bundles them (Linux/macOS installer installs) but fails on the engineering package.
- Installer is `curl | sh`; not used (violates the spec's no-untrusted-auto-update rule). Binary pinned and stamped instead.
- Not reviewed: Pi-Bolt source, its network behavior at runtime, `pi-bolt update`/`doctor` network calls, build provenance beyond the signature.

## Gap matrix
| Feature | Current implementation | Pi-Bolt mechanism | Risk | Test | Decision |
|---|---|---|---|---|---|
| Launch / selection | managed wrapper + node launcher | child process behind launcher, YAML `pi_runtime` | low | unit + launcher integration (pass) | implemented, inert by default |
| Fallback before start | none | selection refuses unsupported/unpinned/changed binary | low | pass | implemented |
| No replay after start | n/a | exit code propagated, no second runtime | low | pass | implemented |
| Session restore | `pi --session`/`--continue` | same Pi code | low | round-trip PASS (4 hops) | ok |
| Extension loading | package dirs via settings | jiti + interpreter | HIGH (5-8x time, ~13x CPU) | probe + cost reps | blocks broad use |
| Bundled extensions | n/a | `pi-bolt compile` | blocked | compile attempts | open |
| Provider/auth/streaming (InferWeave) | models.json provider | same | low | no-op/tools/concurrency runs | ok; accounting not verified |
| Plan gates / Autospec / Planotator | extension code | same code, slower load | unknown | not run under Pi-Bolt | open (#186) |
| Collaboration / lineage / issue graph | extension + control plane | same code | unknown | not run | open (#187) |
| Dashboard metrics | runtime events | `runtimeFacts()` on events | medium | unit test; worker->ingest wiring missing | partial (#191) |
| macOS / beast / Apptainer | various | Apple silicon only | medium | not run | stay on standard Pi |
| Supply chain | npm pi | signed release, LGPL parts | medium | checksum + signature | review before any approval |
