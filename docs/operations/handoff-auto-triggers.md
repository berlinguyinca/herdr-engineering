# Automatic handoff triggers (design note, opt-in, not installed)

`/handoff` is manual only. Nothing in this repository edits any user settings, hooks or config.
This note records how a user could opt in to automatic triggers later, and what is not verified.

All of these would run the same two calls the skill runs (`herdr-handoff create`, then `launch`),
or ask the agent to run `/handoff`. A hook cannot compose the briefing itself: the briefing needs
the agent's judgement, so a hook should only *prompt* the agent or write a minimal envelope.

| Harness | Mechanism | What is known | Not verified |
| --- | --- | --- | --- |
| Claude Code | `PreCompact` hook in `settings.json` (matcher `manual` / `auto`) | Hooks run a command with JSON on stdin (transcript path, trigger) before compaction; a hook can run `herdr-eng summary history --json --session <id>` read-only. | Whether hook stdout can inject context before compaction in the user's version. |
| Codex | auto-compaction (`auto_compact` style settings in `config.toml`) | Codex compacts by token limit. | Any pre-compaction hook; check `codex --help` and the config reference before relying on it. |
| OpenCode | plugin events (session compaction) | OpenCode has a plugin system with session events. | The exact event name and whether it fires before compaction. |
| Pi | `contextHandoffTrigger` in pi-engineering | Pi already has a context-handoff trigger. | Its threshold semantics; Pi's built-in `/handoff` stays a separate planner-worker feature. |

Guidance if enabling any of them:

* Keep the trigger idempotent: key by session id and skip when a handoff for the session is already `queued`.
* Prefer a warning ("context 80% full, run /handoff") to an unattended launch; a launch opens a new agent that spends tokens.
* Never let a hook write settings itself; an opt-in snippet belongs in the user's own config under their review.
