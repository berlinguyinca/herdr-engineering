# Live provider evidence (2026-11-12)

Real runs against a local throwaway control plane (`server.py`: in-memory repo, throwaway ingest/operator keys, 127.0.0.1 only).
Binaries present: pi 1.3.0, claude 2.1.306, codex-cli 0.167.1, herdr 0.9.1.

| Provider | Result | Mechanism |
|---|---|---|
| Pi | Real model turn ran: model `metabolomics/qwen3.8-flash_next`, reply `ok` (`pi-real-reply.out`), the map showed 1 agent at ctx 2% (`pi-real-frame.txt`), using `--no-extensions --no-mcp --no-skills`. A first run with default extensions hung until timeout with no events; diagnosis found no adapter fault: `pi -p` waits for stdin EOF when stdin is not a TTY (reproduced with and without the adapter, any flags), and a heavily loaded host (load 40-60) stretched the extension runtime start from ~4 s to 10-55 s. Run Pi with `</dev/null` and a generous timeout; with default extensions and `</dev/null` the adapter delivered events (provider pi, ctx 4431/131072). Earlier no-credential run: extension loaded, session.started + session.ended only. | `pi -e pi.js -p --no-extensions --no-mcp --no-skills` (`run-pi.sh`) |
| Claude | Ran: real haiku turn via `--settings` hooks (session-only; user settings untouched; `--setting-sources project`). Model and context unknown: Claude hook payloads carry no model (`hook-claude-payload-keys.txt`). | `run-claude.sh` |
| Codex | Ran: real `codex exec` with `-c notify=[...]` override (config.toml untouched), then `codex.js rollout <file>`. Model `gpt-6-astra`, context 26981/828400 are real values from the rollout. | `run-codex.sh` |
| Herdr manifest | `herdr plugin link` succeeded against a throwaway HERDR_HOME/XDG/HOME; real ~/.config/herdr/plugins.json unchanged (`link.out`). | `run-link.sh` |

snapshot.json holds 5 agents (Claude was run 3 times to capture hook payload keys). Labels are null. No paths, prompt text,
token strings, transcript or cwd in snapshot or events (grep: 0 hits).
The installed `herdr-eng` on PATH predates `agent-ops`; the TUI check used `python -m herdr_engineering.cli agent-ops --once`.
