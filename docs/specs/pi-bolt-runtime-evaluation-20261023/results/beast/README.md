# Quiet-host re-run on beast (2026-10-23, 17:42-18:05)
Host: beast (24 cores, load 26-32 throughout; bender's was 78-270). Same binaries (Pi-Bolt bolt-v1.0.0 sha256 verified identical on arrival, Pi 1.4.0 baseline, node 22.22.1),
same model and gateway, beast's own provider config (no key was copied). Driver: the same Rust `pi-runtime-bench`, arms interleaved. Raw rows: `*.jsonl`, summaries `*.txt`.

| Scenario | standard-pi | pi-bolt | pi-bolt-jit |
|---|---|---|---|
| `--version` wall (12 reps) | 363 ms | 17 ms | 19 ms (CIs disjoint) |
| no-op CPU s (10 reps) | 0.83 | 0.07 | 0.09 |
| no-op peak RSS | 121 MiB | 112 MiB | 112 MiB |
| no-op total wall | 2265 ms | 1701 ms | 1731 ms (not clearly different) |
| tools wall / tool time / quality | 17.8 s / 122 ms / 10/10 | 22.5 s / 3 ms / 10/10 | 21.8 s / 7 ms / 10/10 (wall not clearly different) |
| 24 concurrent no-op (3 reps, 72 runs/arm) wall | 16.2 s | 15.1 s | 17.8 s (not clearly different), 72/72 ok each |
| **Real extension set, ready time (5 reps)** | 6.1-6.8 s | 26.1-28.2 s | 25.5-28.3 s |
| Real extension set, CPU s | 2.7-3.4 | 22.7-25.1 | 22.2-25.0 |
| Real extension set, peak RSS | 139-160 MiB | 426-452 MiB | 433-463 MiB |

Findings versus the loaded-host run on bender:
- Extension-set result CONFIRMED on a quiet host and no watchdog hits: Pi-Bolt is ~4.3x slower to ready, ~8x the CPU and ~3x the memory. 5/5 runs complete in every arm.
- CPU and startup advantage for extension-free agents CONFIRMED (~12x lower CPU, 21x faster `--version`).
- The ~2.4x RSS advantage seen on bender does NOT reproduce here (112 vs 121 MiB no-op). Treat the memory saving as unproven.
- Total wall time is not clearly different in any model-bound scenario, as on bender.
Extension set loaded here: engineering release ef5130f2 only (bender's run also had build-gate and herdr-agent-state).
