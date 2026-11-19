# Automatic handoff triggers (design note, opt-in, not installed)

`/handoff` is manual only. Nothing in this repository edits any user settings, hooks or config.
This note records how a user could opt in to automatic triggers later, and what is not verified.

All of these would run the same two calls the skill runs (`herdr-handoff create`, then `launch`),
or ask the agent to run `/handoff`. A hook cannot compose the briefing itself: the briefing needs
the agent's judgement, so a hook should only *prompt* the agent or write a minimal envelope.

| Harness | Mechanism | What is known | Not verified |
| --- | --- | --- | --- |
| Claude Code | `PreCompact` hook in `settings.json` (matcher `manual` / `auto`) | Hooks run a command with JSON on stdin (transcript path, trigger) before compaction; a hook can run `herdr-eng summary history --json --session <id>` read-only. | Whether hook stdout can inject context before compaction in the user's version. |
| Codex | auto-compaction (`auto_compact` style settings in `config.toml`) | Codex compacts by token limit; the rollout records a `compacted` record and the context size as `last_token_usage.total_tokens` (measured, see [capabilities](handoff-harness-capabilities.md#codex)). | Any pre-compaction hook; check `codex --help` and the config reference before relying on it. |
| OpenCode | plugin events (session compaction) | OpenCode has a plugin system with session events. | The exact event name and whether it fires before compaction. |
| Pi | `contextHandoffTrigger` in pi-engineering | Pi already has a context-handoff trigger. | Its threshold semantics; Pi's built-in `/handoff` stays a separate planner-worker feature. |

## Context size and window (measured)

Settled by measurement on 2026-11-18; the field names, sources and model ids are in
[handoff-harness-capabilities.md](handoff-harness-capabilities.md):

* Claude Code: transcripts record no window. Claude Code's cached model catalogue lists `runtime.max_input_tokens` per model
  (`window_source='recorded'`); `MODEL_WINDOWS` covers the models seen when it is absent.
* Codex: `info.model_context_window` is recorded on every `token_count` event; the current context is `last_token_usage.total_tokens`.
* Pi: the window is Pi's `models.json` `contextWindow`, not a session record. Some quantised model ids are not listed there and fall back to 128,000.
* OpenCode: the database holds no window; OpenCode's config, then its cached catalogue, list `limit.context`.

Still not verified, measured by Task 7: how to detect an input-ready prompt in each harness, and each harness's exit command and timeout.

Guidance if enabling any of them:

* Keep the trigger idempotent: key by session id and skip when a handoff for the session is already `queued`.
* Prefer a warning ("context 80% full, run /handoff") to an unattended launch; a launch opens a new agent that spends tokens.
* Never let a hook write settings itself; an opt-in snippet belongs in the user's own config under their review.

## Subagent registry hooks (installed by default)

`scripts/install.sh` step 7c2 adds the managed `SubagentStart` and `SubagentStop` hooks to `~/.claude/settings.json`
(`herdr-eng handoff auto install --subagents-only`). They record running background agents per pane so the handoff drain
knows whether to wait. Each command is guarded `[ -x ... ] && ... || true`, so a missing binary or any failure exits 0 and
never disturbs a session. Opt out with `--no-subagent-hooks` or `HERDR_INSTALL_SUBAGENT_HOOKS=0`; remove them with
`herdr-eng handoff auto uninstall`. Foreign hooks are untouched. Only Claude Code emits these events.

### Live check

`tests/e2e/test_subagent_registry_live.py` (marker `herdr_live`, off by default) starts interactive Claude in a pane it
splits off, asks it for one background agent that sleeps about 20 seconds, and polls
`herdr-eng handoff subagents --pane <id> --status --json` until `live == 1`, then `live == 0`. It skips unless a Herdr
socket and `HERDR_PANE_ID` are present, `claude` and `herdr-eng` are installed, and the managed hooks (marker
`herdr-handoff-auto`) are already in `~/.claude/settings.json`; it only reads that file. After installing the hooks, run it
from a Herdr pane, with `HERDR_LIVE_CWD` set to a folder Claude already trusts:

```
pytest tests/e2e/test_subagent_registry_live.py -m herdr_live -v
```

Observed hook payload keys: to be recorded after the first live run (not yet measured; the registry reads `agent_id` and
`agent_type` and ignores anything else).
