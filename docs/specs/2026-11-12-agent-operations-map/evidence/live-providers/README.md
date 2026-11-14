# Live provider evidence (2026-11-12)

Real runs against a local throwaway control plane (`server.py`: in-memory repo, throwaway ingest/operator keys, 127.0.0.1 only).
Binaries present: pi 1.3.0, claude 2.1.306, codex-cli 0.167.1, herdr 0.9.1.

| Provider | Result | Mechanism |
|---|---|---|
| Pi | Real model turn ran: requested model `metabolomics/qwen3.8-flash_next`, reply `ok` (`pi-real-reply.out`); the map showed 1 agent, ctx 2%, with the model still shown as `?` (`pi-real-frame.txt`: the adapter does not report the model for this run). Operator observation, not committed evidence: a first run with default extensions hit the 120 s timeout with no events; a later diagnosis attributed this to `pi -p` waiting for stdin EOF when stdin is not a TTY, and to host load stretching extension start-up. Run Pi with `</dev/null` and a generous timeout. Earlier no-credential run: extension loaded, session.started + session.ended only. | `run-pi-real.sh` (real turn); `run-pi.sh` (offline, no-credential run) |
| Claude | Ran: real haiku turn via `--settings` hooks (session-only; user settings untouched; `--setting-sources project`). Model and context unknown: Claude hook payloads carry no model (`hook-claude-payload-keys.txt`). | `run-claude.sh` |
| Codex | Ran: real `codex exec` with `-c notify=[...]` override (config.toml untouched), then `codex.js rollout <file>`. Model `gpt-6-astra`, context 26981/828400 are real values from the rollout. | `run-codex.sh` |
| Herdr manifest | `herdr plugin link` succeeded against a throwaway HERDR_HOME/XDG/HOME; real ~/.config/herdr/plugins.json unchanged (`link.out`). | `run-link.sh` |

snapshot.json holds 5 agents (Claude was run 3 times to capture hook payload keys). Labels are null. No paths, prompt text,
token strings, transcript or cwd in snapshot or events (grep: 0 hits).
The installed `herdr-eng` on PATH predates `agent-ops`; the TUI check used `python -m herdr_engineering.cli agent-ops --once`.
