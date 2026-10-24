# Decision — Pi-Bolt as an execution runtime for Pi Engineering agents (2026-10-23)

**Recommendation: NO-GO as default or broad rollout. Keep standard Pi as the default everywhere.**
**PB-014 (canary): declined 2026-10-23 ("keep standard Pi"). A narrow canary for extension-free helper agents remains possible later, only after explicit human approval (see "What could be approved").**
No production default or canary has been changed or enabled. PB-014 is not started and needs your approval.

## What was measured (actual data, not vendor claims)
Pi-Bolt `bolt-v1.0.0` (Pi 1.4.0 base; checksum and Ed25519 signature verified) vs Pi 1.4.0 on node 22, same host (bender, linux-x64),
same model (`metabolomics/deepseek_v4-flash…`), isolated agent dirs, arms interleaved per repetition.
Bender's load average was 78-120 throughout, so absolute times are inflated; arms share that noise. Raw rows are in `results/`.
Reps: startup 12, no-op 10, tools 10, concurrency 3 per arm (below the spec's 10, see Limits). Intervals are bootstrap 95% CIs of the median.

### A. Extension-free agent (no extensions loaded)
| Metric (median) | standard-pi | pi-bolt | pi-bolt-jit |
|---|---|---|---|
| `--version` wall | 1432 ms | 94 ms | 64 ms (CIs disjoint from standard) |
| Runtime start to session event | 2708 ms | 182 ms | 233 ms |
| Exit overhead after agent end | 141 ms | 39 ms | 42 ms |
| CPU seconds per no-op run | 0.75 | 0.08 | 0.08 |
| Peak RSS | 123 MiB | 51 MiB | 55 MiB |
| Tool time (10 reads + write, 10 reps) | 320 ms | 55 ms | 31 ms |
| Task quality (tools, sum of 10 files) | 9/10 | 10/10 | 10/10 |
| Total wall time, no-op / tools / 5 and 12 concurrent | not clearly different in any scenario (model and gateway variance dominates; time to first token ranged 835 to 5654 ms across arms for identical requests) |

The JIT arm's one "clearly lower wall time" in the no-op run is gateway variance (its time to first token was 835 ms vs 5654 ms), NOT a runtime effect, and is not claimed.
Quality 9/10 vs 10/10 is within model nondeterminism at n=10; no quality regression or gain is claimed.

### B. Real Pi Engineering agent (engineering release ef5130f2 + build-gate + herdr-agent-state loaded)
| Metric | standard-pi | pi-bolt | pi-bolt-jit |
|---|---|---|---|
| Time to ready (3 reps, s) | 11.0, 19.3, 10.6 | 48.8, 95.8, 80.9 | 57.1, 89.1, 61.9 |
| CPU seconds to ready (n=1 raw for standard; see results/README.md for provenance) | 1.9 | 24.4, 24.2 | 24.9 |
| Peak RSS (same caveat) | 141 MiB | 369-384 MiB | 370-377 MiB |
| Commands / tools registered | 44 / 17 | 45 / 17 (+`usage`) | 45 / 17 (+`usage`) |
In the first pass both Pi-Bolt arms hit the 120 s watchdog (no result); in later runs they completed in 49-96 s, and one more JIT run hit the watchdog. Under Pi-Bolt the extensions are loaded at run time (jiti transpile + interpreter), which Pi-Bolt's own
docs flag; the documented fix, `pi-bolt compile`, fails on this package (see compatibility-matrix.md). Net effect for the agents we actually run:
**about 5-8x slower to ready, ~13x the CPU and ~2.6x the memory of standard Pi.** The startup win in A disappears and reverses.

Later repetitions 3-5 ran when host load had risen to 181-270 (other workloads): standard Pi still became ready in 17.6, 28.6 and 20.5 s (1.4-1.8 CPU s, 131-142 MiB);
every Pi-Bolt and Pi-Bolt JIT run hit the 120 s watchdog (6 of 6, ~7 CPU s consumed in 120 s, i.e. the process was CPU-starved). These are
censored and heavily load-confounded: they support the direction (Pi-Bolt does not finish loading this extension set where standard Pi does) but must not be
used for magnitudes. Magnitudes above come from the lower-load repetitions 1-2.

### C. Quiet-host re-run on beast (load ~27 on 24 cores; results/beast/README.md)
Extension-free: `--version` 363 ms vs 17 ms, no-op CPU 0.83 s vs 0.07 s, 24-concurrent and tools wall not clearly different, all runs ok. **Real extension set, 5/5 runs complete: 26-28 s vs 6.1-6.8 s to ready (~4.3x slower), CPU 23-25 s vs 2.7-3.4 s (~8x), RSS ~440 vs ~145 MiB (~3x).** The earlier ~2.4x RSS saving for extension-free agents did not reproduce (112 vs 121 MiB), so only the CPU and startup advantage is established. This answers the loaded-host caveat: the NO-GO for agents with the extension set holds on a quiet host.

## Correctness and compatibility
- Session round-trip PASS: standard Pi -> Pi-Bolt -> standard Pi -> Pi-Bolt JIT on one session file; the remembered fact survived every hop, one session file, valid JSONL after each hop (`results/session-roundtrip.txt`). (A first attempt failed because my harness used the wrong resume flag; it failed on standard Pi too and was fixed.)
- InferWeave provider URL, auth, model id and token streaming worked unchanged through Pi-Bolt in every scenario (gateway responses streamed; no auth or routing errors).
  Wire parity vs a local mock provider (`pi-runtime-bench parity`, results/parity.txt): same path, same auth credential, same request body apart from the system prompt (it names Pi-Bolt, its doc paths and the cwd). Headers differ: `user-agent` says `bun/1.4.3` instead of `node/v22.23.3`, `x-stainless-runtime-version`, `accept-encoding`, and two browser-style headers are absent. **A gateway rule keyed on user-agent or runtime headers would see Pi-Bolt as a different client**; check the gateway's identity-header handling before any canary.
  NOT verified: gateway-side accounting/billing rows, quotas, owner priority, session affinity (needs gateway-side data).
- Extension registration identical apart from Pi-Bolt's own `usage` command.
- Adapter safety tested (pi-engineering `feat/pi-bolt-runtime-adapter`): 7 unit + 6 launcher integration tests, existing 28 session-auto-resume tests still pass.
  Covered: precedence, deterministic canary, unsupported host, unpinned/changed/mis-versioned binary all fall back to standard Pi before start,
  a started runtime's exit code is propagated and never replayed on standard Pi, malformed config never blocks Pi.

## Open item: per-client gateway accounting (unverified, access-gated)
Not checked: what the gateway recorded per client or per session for Pi-Bolt traffic. The authorized Pi key reaches only global `/v1/usage` totals; per-client rows need operator or database access that was not granted. Established instead: identical wire request apart from the system prompt, identical client-visible usage and caching, and a different `user-agent` (bun) that could change attribution. Anyone approving a future canary should check the gateway's per-client rows for the canary host first.

## Where Pi-Bolt can apply (important scope finding)
Mission workers run in-process through the Pi SDK (`PiWorkerExecutor` -> `createAgentSession`), not through a spawned `pi` binary, and the pane-based `HerdrAgentRuntime` has no production caller. The launcher selector therefore covers only agents started with the `pi` command. Plan gates, Autospec/Planotator execution and lineage run on paths Pi-Bolt does not touch. Pi-Bolt's RPC mode works (live check) and is the route if mission workers ever need it. A spike (`PiBoltRpcWorkerExecutor`, pi-engineering, 6 tests, not wired) ran a real scout task through Pi-Bolt and standard Pi with identical results, so RPC workers are feasible for simple read-only tasks. It does not carry over the terminating tools, resilience wrapper or context budgets that gates and lineage rely on, so those stay on the SDK executor. Details: results/mission-worker-scope.txt.

## Acceptance criteria status
| Criterion | Status |
|---|---|
| 100% pass of lifecycle/session/permissions/plan gates/collaboration/issue linkage/routing/accounting/trace tests | PARTIAL: adapter, fallback, session restore, worker->control-plane->dashboard pass; existing collaboration tests (45) pass because that layer is runtime-independent; plan gates, issue linkage and lineage cannot run under Pi-Bolt today (SDK in-process workers, see scope finding); accounting only at the wire level |
| No unreviewed security regressions, missing audit events, corrupted sessions | Sessions OK; security review of the binary's network behavior NOT done |
| Statistically credible improvement in an important bottleneck without quality regression | Only for extension-free agents (CPU, RSS, startup: yes). For real engineering agents: regression |
| Rollback to standard Pi without data loss or duplicate side effects | PASS live through the real launcher (results/rollback-drill.txt) and after kill -9/SIGTERM (results/cancel-drill.txt); NOT drilled inside a live Herdr mission |
| Incompatibilities documented; unsupported hosts stay on standard | Yes (compatibility-matrix.md); automatic fallback implemented |
| Production rollout requires human approval | Yes; nothing enabled |

## Limits of this evidence (read before deciding)
- The bender runs were heavily loaded (load 78-270). The key scenarios were re-run on beast at load ~27 (section C); the conclusions held, except the RSS saving.
- Spawner env export verified in a real herdr pane; that check exposed wrong `agent start`/`agent wait` flags in the existing client, fixed with a pinning test (results/herdr-and-accounting-checks.txt).
- Worker -> control plane -> dashboard wiring proven end to end against a real local control plane (results/e2e-runtime-dashboard.txt).
- Live collaboration: a real Pi-Bolt agent joined a real local room over MCP, was killed mid-session, a new process reconnected, and a client resuming from its old cursor saw each message exactly once (results/collab-live.txt). `--no-extensions` hides MCP tools under Pi-Bolt.
- Client-visible gateway usage is parsed and cached identically by both runtimes; Pi-Bolt sends ~69 more input tokens (longer system prompt) (results/usage-reporting.txt). Per-client gateway rows remain unchecked (operator access).
- Cancel/crash: one manual extension-free drill PASSED (kill -9, SIGTERM, resume on standard Pi; results/cancel-drill.txt). Not a live mission.
- Churn soak: 240 launches at 6-way concurrency, 0 failures, no RSS growth (results/soak-churn.txt); a 21-minute interactive single-session soak also showed flat memory (58-60 MiB over 22 turns, results/long-session-soak.txt). A 2-hour RPC soak with the real extension set on beast (results/beast/soak-2h-rpc.txt): Pi-Bolt flat at ~195 MiB, standard Pi crept 138 -> 163 MiB, no instability in either arm.
- 24 concurrent no-op agents: 48/48 ok per arm; Pi-Bolt wall clearly lower, CPU ~10x lower (results/noop-c24.txt).
- Not run: full Herdr mission with child agents and issue links (mission workers are in-process SDK, see scope finding),  gateway-side accounting rows (wire parity done), Autospec/Planotator/lineage gates under Pi-Bolt, extension-failure injection, macOS/Apptainer hosts (beast, linux-x64, was run), OpenViking (not installed on bender), true cold start (page cache not dropped).
- Concurrency (5, 12) used 3 repetitions; interval estimates are wide. Tools and extension-load reps (3) are below the spec's 10 because each extension-loaded run takes minutes on this host.
- Bundled mode (`pi-bolt compile`) was attempted and is blocked; it is the one mode that could remove the extension-load penalty.
- Single model; reasoning-heavy models and long contexts untested.

## What could be approved (nothing is approved yet)
1. Do nothing: standard Pi stays default. This is the recommended default.
2. Narrow canary, extension-free only: one role that runs without the engineering extension (for example short-lived read-only helpers), `pi-bolt` (not JIT), bender only, `canary.percent` small, rollback by deleting the config entry. Expected benefit from A: ~10x lower CPU and ~2.4x lower RSS per helper at 12+ concurrent agents. Needs: confirmation that such a role exists and runs without extensions, and a quiet-host re-measurement.
3. Revisit broad use only if `pi-bolt compile` can bundle the engineering extensions and a quiet-host benchmark shows ready-time and CPU at or below standard Pi.

## Next steps requested
- Approval decision on option 2 (PB-014), or "no".
- Optional: ask the Pi-Bolt maintainers about the silent `pi-bolt compile` failure and the optional-import rule; fix `BlackholeAdapter` optional import to be bundler-safe if bundled mode is pursued.
