# Raw results (all produced by bench/pi-runtime-bench, Rust)
- `startup-version-rust.jsonl` — `--version`, 12 reps. 
- `ttft-noop-rust.{jsonl,txt}` — no-op through the gateway, 10 reps, phase split.
- `tools-c1.{jsonl,txt}` — read 10 files, write sum, 10 reps, result checked.
- `noop-c5.*`, `noop-c12.*` — 5 and 12 concurrent agents, 3 reps.
- `compat.jsonl`, `compat-extload.jsonl` — extension sets loaded under each arm (commands, tools, time, CPU, RSS). `compat.jsonl` includes first-pass runs from a probe with a timing bug (user-extension set reported 0 commands in all arms) and runs that hit the 120 s watchdog.
- `session-roundtrip.txt` — standard -> bolt -> standard -> bolt-jit on one session.
Host load average during all runs: 78-120 (see `loadavg1` per row).
