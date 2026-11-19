# Automatic handoff: one policy

The automatic handoff controller (`herdr-eng handoff controller run`, described in
[handoff.md](handoff.md#automatic-handoff-controller)) is the only thing that decides when a session hands off. It
watches Claude Code, Codex, Pi and OpenCode sessions that Herdr reports and acts at 80 percent of the context window, with a per-session
switch (`herdr-eng handoff auto off|on`) and a global one (`HERDR_HANDOFF_AUTO=0`). The `/handoff` skill stays for people who
hand off by hand.

Two older policies are gone:

* the 50 percent `UserPromptSubmit` reminder (`herdr-eng handoff context-hook claude`) and the `/handoff` text in the
  managed `SessionStart` and `PreCompact` hooks of `herdr-eng workflow install`;
* autospec's tmux context monitor (50 percent compact, 80 percent rollover) for sessions started from a Herdr pane.

No hook of this repository asks an agent to run `/handoff` because of context size any more, and nothing in this repository
edits user settings except the installers the user runs.

## Upgrading

Run both once, on each host:

```
herdr-eng handoff auto install
herdr-eng workflow install
```

They remove the old entries from `~/.claude/settings.json` (and add the current ones), each with a backup, atomically, and
only for a command that carries our marker (`herdr-handoff-auto` plus `handoff context-hook`, or `herdr-workflow:managed`
plus `workflow hook PreCompact`). Foreign hooks and hand-edited entries stay. A second run changes nothing. Nothing runs this
migration on its own; `scripts/install.sh` runs both. `herdr-eng handoff auto check` reports a still-installed old hook as
`stale` with the command to fix it, and `herdr-eng workflow doctor` reports the old `PreCompact` entry as drift.

Until you do, the old commands are harmless: `herdr-eng handoff context-hook <anything>` and `herdr-eng workflow hook PreCompact`
are a silent no-op (they read stdin, print nothing, exit 0), so a session with the old hooks installed is not disturbed and no
longer gets a reminder.

autospec's monitor skips a session that was started from a Herdr pane (`AUTOSPEC_CONTEXT_MONITOR_SKIP_HERDR`, default on).
The skip is keyed on `HERDR_PANE_ID` being set in the session's environment: a shell where it is unset, such as a remote shell
that reaches a host over ssh, is not a Herdr session to the monitor and keeps the old behaviour. The consequence for a Herdr pane
whose controller is switched off (`herdr-eng handoff auto off`, or `HERDR_HANDOFF_AUTO=0`): the monitor stays off for that pane
too, so such a pane has no automatic policy at all and only a person's `/handoff` moves it. Outside Herdr (plain tmux) the
monitor is unchanged.

## Mechanisms measured for each harness (record)

These were the options looked at before the controller; the controller does not use hooks to trigger. What is not verified is
noted.

| Harness | Mechanism | What is known | Not verified |
| --- | --- | --- | --- |
| Claude Code | `PreCompact` hook in `settings.json` (matcher `manual` / `auto`) | Hooks run a command with JSON on stdin (transcript path, trigger) before compaction; a hook can run `herdr-eng summary history --json --session <id>` read-only. | Whether hook stdout can inject context before compaction in the user's version. |
| Codex | auto-compaction (`auto_compact` style settings in `config.toml`) | Codex compacts by token limit; the rollout records a `compacted` record and the context size as `last_token_usage.total_tokens` (measured, see [capabilities](handoff-harness-capabilities.md#codex)). | Any pre-compaction hook; check `codex --help` and the config reference before relying on it. |
| OpenCode | plugin events (session compaction) | OpenCode has a plugin system with session events. | The exact event name and whether it fires before compaction. |
| Pi | `contextHandoffTrigger` in pi-engineering | Pi already has a context-handoff trigger. | Its threshold semantics; Pi's built-in `/handoff` stays a separate planner-worker feature. That trigger lives in pi-engineering, not in this repository, so this repository installs and removes nothing for Pi or OpenCode; do not run it next to the controller. |

## Context size and window (measured)

Settled by measurement on 2026-11-18; the field names, sources and model ids are in
[handoff-harness-capabilities.md](handoff-harness-capabilities.md):

* Claude Code: transcripts record no window. Claude Code's cached model catalogue lists `runtime.max_input_tokens` per model
  (`window_source='recorded'`); `MODEL_WINDOWS` covers the models seen when it is absent.
* Codex: `info.model_context_window` is recorded on every `token_count` event; the current context is `last_token_usage.total_tokens`.
* Pi: the window is Pi's `models.json` `contextWindow`, not a session record. Some quantised model ids are not listed there and fall back to 128,000.
* OpenCode: the database holds no window; OpenCode's config, then its cached catalogue, list `limit.context`.

Still not verified, measured by Task 7: how to detect an input-ready prompt in each harness, and each harness's exit command and timeout.

## Subagent registry hooks (installed by default)

`scripts/install.sh` step 7c2 adds the managed `SubagentStart` and `SubagentStop` hooks to `~/.claude/settings.json`
(`herdr-eng handoff auto install --subagents-only`). They record running background agents per pane so the handoff drain
knows whether to wait. Each command is guarded `[ -x ... ] && ... || true`, so a missing binary or any failure exits 0 and
never disturbs a session. The step is skipped, and `settings.json` is not created, on a host without Claude Code (no `claude`
on `PATH` and no `~/.claude`), the same gate as the workflow hooks. Opt out with `--no-subagent-hooks` or `HERDR_INSTALL_SUBAGENT_HOOKS=0`; remove them with
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
