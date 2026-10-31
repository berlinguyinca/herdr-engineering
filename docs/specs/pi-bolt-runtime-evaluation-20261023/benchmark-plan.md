# Benchmark plan — Pi vs Pi-Bolt (standard, JIT)

Driver: `bench/pi-runtime-bench` (Rust; pure core with unit tests, thin process layer). Arms: `bench/runtimes.json`.
Build and test: `cd bench/pi-runtime-bench && cargo test && cargo build --release`.
Raw rows (one JSON object per run) land in `results/*.jsonl`; the driver prints median, p95 and a bootstrap 95% CI of the median
(deterministic seed) and only calls a difference "clear" when the arms' intervals are disjoint.

## Fairness rules
- Arms are interleaved within each repetition so host noise lands on every arm. 1 warmup + >= 10 measured repetitions per arm.
- Same host, model, prompt, env (cleared), cwd contents and isolated agent dir per arm. Pi 1.4.0 baseline matches the Pi version Pi-Bolt 1.0.0 is built on.
- Nothing disabled in one arm only: `compat` and the extension scenarios load the same package set under every arm.
- Model and gateway time are reported separately (`model_ttft_ms`, `model_total_ms`) from runtime-attributable phases
  (`runtime_start_ms` spawn to session event, `prep_ms` request build, `exit_ms` agent end to process exit). Only the latter, plus
  CPU and RSS, are evidence about the runtime.
- Cold = first run after unpack; the OS page cache is not dropped (no root), so true cold start is NOT measured.
- Host load is recorded per row (`loadavg1`). Bender's load average was 78-96 during these runs: absolute times are inflated and noisy; compare arms, not absolutes.

## Scenarios
| Scenario | Command | Status |
|---|---|---|
| Startup (`--version`, `--help`) | `pi-runtime-bench version` / `help` | run |
| No-op ready-to-first-token | `pi-runtime-bench noop --model … --models-json …` | run |
| Tool-heavy (read 10 files, write sum; quality checked) | `pi-runtime-bench tools …` | see results |
| Concurrency 1/5/12/24 | `--concurrency N` on noop/tools | see results |
| Extension load / registration diff | `pi-runtime-bench compat --package P …` | see compatibility-matrix.md |
| Full Herdr mission, collaboration room, soak, crash recovery | needs Herdr panes + rooms | NOT run here (see decision.md) |

## Safety of the harness
The credential file is copied into a private 0600 temp dir removed on exit (Drop), is never written to results, and the child env is cleared.
Models used are cheap single-turn calls through the configured gateway.
