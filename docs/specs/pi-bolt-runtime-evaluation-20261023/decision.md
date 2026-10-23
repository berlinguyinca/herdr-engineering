# Decision — Pi-Bolt as an execution runtime for Pi Engineering agents (2026-10-23)

**Recommendation: NO-GO as default or broad rollout. Keep standard Pi as the default everywhere.**
**Optional, only after explicit human approval: a narrow canary for extension-free helper agents (see "What could be approved").**
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
| CPU seconds to ready | 1.9 | 24.4, 24.2 | 24.9 |
| Peak RSS | 141 MiB | 369-384 MiB | 370-377 MiB |
| Commands / tools registered | 44 / 17 | 45 / 17 (+`usage`) | 45 / 17 (+`usage`) |
In the first pass both Pi-Bolt arms hit the 120 s watchdog (no result); in later runs they completed in 49-96 s, and one more JIT run hit the watchdog. Under Pi-Bolt the extensions are loaded at run time (jiti transpile + interpreter), which Pi-Bolt's own
docs flag; the documented fix, `pi-bolt compile`, fails on this package (see compatibility-matrix.md). Net effect for the agents we actually run:
**about 5-8x slower to ready, ~13x the CPU and ~2.6x the memory of standard Pi.** The startup win in A disappears and reverses.

## Correctness and compatibility
- Session round-trip PASS: standard Pi -> Pi-Bolt -> standard Pi -> Pi-Bolt JIT on one session file; the remembered fact survived every hop, one session file, valid JSONL after each hop (`results/session-roundtrip.txt`). (A first attempt failed because my harness used the wrong resume flag; it failed on standard Pi too and was fixed.)
- InferWeave provider URL, auth, model id and token streaming worked unchanged through Pi-Bolt in every scenario (gateway responses streamed; no auth or routing errors).
  NOT verified: gateway-side accounting/billing rows, quotas, owner priority, session affinity.
- Extension registration identical apart from Pi-Bolt's own `usage` command.
- Adapter safety tested (pi-engineering `feat/pi-bolt-runtime-adapter`): 7 unit + 6 launcher integration tests, existing 28 session-auto-resume tests still pass.
  Covered: precedence, deterministic canary, unsupported host, unpinned/changed/mis-versioned binary all fall back to standard Pi before start,
  a started runtime's exit code is propagated and never replayed on standard Pi, malformed config never blocks Pi.

## Acceptance criteria status
| Criterion | Status |
|---|---|
| 100% pass of lifecycle/session/permissions/plan gates/collaboration/issue linkage/routing/accounting/trace tests | PARTIAL: adapter, fallback and session restore pass; plan gates, collaboration, issue linkage, accounting, trace continuity NOT run under Pi-Bolt (need a full Herdr mission) |
| No unreviewed security regressions, missing audit events, corrupted sessions | Sessions OK; security review of the binary's network behavior NOT done |
| Statistically credible improvement in an important bottleneck without quality regression | Only for extension-free agents (CPU, RSS, startup: yes). For real engineering agents: regression |
| Rollback to standard Pi without data loss or duplicate side effects | Config rollback and no-replay tested at the launcher level; NOT drilled with live missions |
| Incompatibilities documented; unsupported hosts stay on standard | Yes (compatibility-matrix.md); automatic fallback implemented |
| Production rollout requires human approval | Yes; nothing enabled |

## Limits of this evidence (read before deciding)
- Host was heavily loaded (load 78-120) for the whole run; absolute numbers are not representative of an idle host. Re-run on a quiet host before any approval.
- Not run: 24-agent concurrency, full Herdr mission with child agents and issue links, collaboration room with reconnect, long soak, crash recovery, extension-failure injection, macOS/beast/Apptainer hosts, OpenViking (not installed on bender), steady-state CPU/RSS after load, true cold start (page cache not dropped).
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
