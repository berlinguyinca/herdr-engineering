---
name: herdr-join
description: Join this agent's HerdR workspace room over the tokenless tailnet fabric-trust path, with no token and no owner click. Use when asked to join the workspace room, share a room with the other agents in this HerdR workspace, or run /herdr::join, in Claude Code, Codex, OpenCode or Pi.
---

# Join the HerdR workspace room

Usage: `/herdr::join [--no-workspace]` (Codex: `$herdr-join`). This skill wraps the existing `herdr-eng rooms join-fabric` CLI; it does not implement a second join path.

How it works: the control plane's fabric listener asks tailscaled who the TCP peer is (whois). That tailnet machine is the only credential, so nothing secret is sent or stored. The room key is `(whois machine, HERDR_WORKSPACE_ID)`: every agent started in the same HerdR workspace on the same machine lands in ONE shared room, and the same workspace id on another machine is a different room. The machine is never client supplied.

## 1. Read the pane context (read-only)

Run `printenv HERDR_WORKSPACE_ID HERDR_TAB_ID HERDR_PANE_ID HERDR_WORKSPACE_LABEL` (a variable that is unset prints nothing).

- No `HERDR_WORKSPACE_ID`: this is not a HerdR pane. Say so. The join still works but creates a plain fabric agent with no room; ask the user whether they want that before continuing.
- Tell the user which workspace id, tab and pane you found. Tab, pane and label are display-only.

## 2. Choose the agent name

Names follow `[a-z0-9][a-z0-9._-]{0,63}`. Use `<harness>-<pane>`, for example `claude-p_3` (harness from `herdr-eng handoff harness`, which prints `{"harness": ...}`; exit 2 means unknown, so ask the user for a name). Lower-case it and replace anything outside `[a-z0-9._-]` with `-`. If the user gave a name, use that instead.

An agent belongs to at most one workspace room: re-joining the same name from a DIFFERENT workspace answers 409. Use another name rather than retrying.

## 3. Join

```bash
herdr-eng rooms join-fabric NAME --json
```

- `--no-workspace` opts out of the room: the agent joins with no workspace and no room. Pass it when the user says so. Otherwise do not pass it.
- The CLI finds the fabric listener itself (host config, `fabric_join` in `~/.config/herdr-engineering/config.yaml`, or the fleet config). Do not ask the user for it and do not pass `--control-plane` unless the command fails saying the fabric listener is unknown; then suggest `herdr-eng summary install` (it records the listener) and only as a last resort ask for `http://<listen_host>:<listen_port>` and pass it with `--control-plane`. The public site never serves this route.
- `--machine M` is only a hint; whois decides.
- The join is idempotent: running it again returns the same agent id.

On success the CLI writes `~/herdr-agents/NAME.json` (`$HERDR_AGENT_DIR` honoured), mode 0600, holding only `agent_id`, `name`, `machine` and, when a room was joined, `room_id`. It holds no credential.

## 4. Report

From the JSON answer (and the identity file) print:

- agent id, name and machine;
- the room: `<machine>/<workspace id>` and its `room_id`. If the answer has `"room_access": false`, say "joined, but the owner removed this agent's workspace-room access" and that nothing was re-granted. If there is no `workspace` at all, say no room was joined and why (opted out, no workspace id, or an older control plane without workspace rooms);
- the permissions the server grants a workspace room: `read`, `post`, `instruct`, `execute`. This list is the server's fixed policy; the CLI answer does not echo the live grant, so do not claim more than that.

State plainly: `coordinate` and `grant_agent` are owner-only. A tokenless auto-joined agent never gets `coordinate` and cannot start tasks for other agents or grant anyone into a room. `instruct` plus `execute` only let it hand on a task a person gave it. The owner can add, narrow or remove access, and those changes survive later joins.

## Refusals

Report the server's error text; never retry with other names to get around it. A caller whose whois machine is not on the tailnet allow-list is refused (401). `auto_join` off, a revoked name (403) or a rate or cap limit (429) are owner decisions.

## Opt-in only

This skill never runs by itself. An owner can turn on a Claude Code session-start auto-join with `fabric_join.auto_join_on_session_start: true` in `~/.config/herdr-engineering/config.yaml` and `herdr-eng summary install`; it is off by default and no other harness gets a hook. Codex needs no hook but one execpolicy rule that lets only `herdr-eng rooms join-fabric` leave its sandbox; `summary install` adds it. If Codex says "control plane is unreachable", run `herdr-eng summary install --check`. Do not add a hook yourself. `herdr-eng summary install --check` reports whether this setup is complete.

## Collisions

If `herdr-join` or a `/herdr-join` command already exists in a harness and was not installed by `herdr-eng summary install`, the installer leaves it untouched and reports `preserved`. The portable command in section 3 works without the skill.
