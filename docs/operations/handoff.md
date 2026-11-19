# HerdR handoffs

`berlinguyinca/herdr-handoff` is the portable client and skill installed into
Codex, Claude, Pi, and OpenCode. It writes private JSON packages to the local
`.herdr/handoffs/outbox/` directory, so a handoff remains possible when the
control plane is unavailable.

It is a required HerdR Engineering operational dependency. Run
`scripts/install.sh` on every host; it installs the pinned revision from
`lock/upstreams.yaml`, the CLI into the project virtualenv, and all four
harness skills. Strict validation fails if any part is missing.

The canonical command namespace is namespaced to avoid collisions with native
harness commands:

```text
/herdr::toClaude
/herdr::toPi
/herdr::toCodex
/herdr::toOpenCode
/herdr::artifact
/herdr::handoff
/herdr::review
/herdr::verify
/herdr::fabric
/herdr::publish
/herdr::observe
/herdr::session
```

No top-level `/artifact`, `/review`, or similar aliases are installed.

## `/handoff [pi|claude|codex|opencode]`

`/handoff` is the history-grounded, default-target form of `/herdr::to*`. It ships with
`herdr-eng summary install` (skill `handoff`, same installer, manifest and "never overwrite what we
did not write" contract as `/summary`; Claude, Codex, OpenCode and Pi get the skill, OpenCode also a
`/handoff` command). It installs **no Pi prompt template**: Pi's built-in `/handoff` is a different
planner-worker feature and keeps its name; in Pi use the skill (`/skill:handoff`).

* Target: the named harness, else the SAME harness as the running session. `herdr-eng handoff harness`
  decides from an explicit name, the nearest ancestor process, the Herdr pane's reported agent, then
  marker environment variables (exactly one may match). The newest-session-in-directory heuristic is
  deliberately not used. When nothing is reliable it exits 2 and the skill asks the user to name one.
* Content: built from `herdr-eng summary history --json` (read-only), git facts and `gh pr list`:
  goal, completed, remaining, decisions, risks, validation, resume point, repo/branch/worktree, PR
  links, key files, exact next commands, constraints and a pointer to the published `/summary` report.
  Unconfirmed items go in `unverified`; secrets are redacted.
* Transport: reuse, not fork. The skill runs `herdr-handoff create ... --from-json -` then
  `herdr-handoff takeover`; `/herdr::to*` runs the same two calls with an explicit target. The envelope is
  `schema_version` 2 (additive, `schemas/handoff-v2.schema.json` in herdr-handoff, which needs
  herdr-handoff 0.4.0 or newer: the skill checks `herdr-handoff --version`). The handover writes the full
  handoff as a markdown document and starts the harness with a one-line prompt telling it to read that
  absolute path first.
* Handover, not duplication: `herdr-handoff takeover` starts the successor in the **same tab** as the
  invoking pane and closes the pane it replaces once herdr has reported the successor live **and** that
  successor has registered (`herdr-handoff confirm`). `launch` still exists and still leaves both panes
  running; use it, or `takeover --keep-source`, when the operator wants the old session kept.
* No pane: without `HERDR_PANE_ID`, `launch` adds a tab to the focused workspace (or creates a
  workspace) in `--cwd`; without a running herdr it prints the exact one-line command
  (`status: manual_launch_required`), leaves the handoff `queued`, and `herdr-handoff resume <id>`
  prints the prompt.
* Per-harness launch: claude, pi and codex take the prompt positionally; opencode takes `--prompt`.
  `--model` is passed to all four (opencode wants `provider/model`; a bare name is dropped with a note).
  Verified against each CLI's `--help` in herdr-handoff's integration tests.

`lock/upstreams.yaml` names the pinned herdr-handoff revision and
`scripts/install-handoff.sh` is the only place that revision is resolved; the pin notes the capabilities
the skills here depend on (v2 envelope, takeover/confirm, takeover-by-default with the worktree guard).

The `to*` commands are interactive: they split the invoking `HERDR_PANE_ID` to
the right, start the target harness in that child pane, send the bounded resume
prompt, and report the handoff/pane metadata back to Herdr. If the current pane
identity is unavailable, the command fails rather than launching a detached
process. Since herdr-handoff 0.4.0 they run `takeover`, so the invoking pane is
closed once the successor is proven live (see *Ordering and refusals*). The
slash commands take only an optional model; when the user says to keep both
sessions running, the skill runs `herdr-handoff takeover --keep-source` instead.

The control plane accepts those packages at `POST /api/v1/handoffs`. The
operator API key is required. A package is queued before a recipient can claim
it; state changes are immutable events and are visible through
`GET /api/v1/handoffs/tree` and the dashboard's Handoffs view.

```sh
herdr-handoff create --goal "Continue the current session" \
  --source-harness codex --target-harness claude \
  --remaining "Run the review" --artifact art_123

herdr-handoff sync --url "$HERDR_CONTROL_PLANE_URL" --token "$HERDR_API_KEY"

# Hand over: start the successor in this tab, then close this pane on proof.
herdr-handoff takeover hnd_123 --cwd "$(git rev-parse --show-toplevel)"
```

The model argument is a hint, not a guarantee. The receiving harness must
report the resolved model. Handoffs do not launch processes or publish
anything automatically. Public sharing remains a separate, human-approved
artifact-publication action.

## Ordering and refusals

Closing a pane destroys an interactive session, so `herdr-handoff takeover` does it last and only on
proof. The pane it replaces is closed after herdr reports the successor live and that successor has
registered the handoff through `herdr-handoff confirm` (the successor registers itself, from the pane it
is actually running in; the wait for herdr is bounded by `--confirm-timeout`, 25 seconds by default).
Before the close it asks git about the source worktree — `--source-cwd`, defaulting to the directory herdr
reports for the source pane and falling back to `--cwd`, because a linked worktree resolves to its own
path and not the upstream checkout:

* `status: source_dirty` — the worktree holds work that is not durable, so the close is refused. The
  successor is live and the handoff succeeded; only the close did not. The reason says which of three
  things git reported: `worktree_dirty` (tracked changes uncommitted), `worktree_unpushed` (commits no
  remote branch has) or `worktree_unknown` (git could not be consulted — missing, not a repo, timed out;
  absence of evidence is not evidence of cleanliness). Commit or push, then run `herdr-handoff takeover
  <handoff_id>` again with the same id: it reuses the successor already recorded for that handoff instead
  of starting a second pane, and closes the source now that git reports it clean.
* `status: launch_unconfirmed` or `launch_failed` — the close was refused and both panes remain up;
  nothing was destroyed. Report it rather than re-running the command to force it.
* `status: source_close_failed` — the handoff completed and herdr refused the close; report it.

Untracked files (`.claude/`, `.venv/`, build output) do not refuse a close: they appear in almost every
checkout, and a guard that cries wolf trains operators to pass `--allow-dirty-source`, which is the flag
that really loses work. It skips only the worktree check — never the live-successor or same-tab checks —
and `git -C <path> status --short` plus `git -C <path> log @{u}..HEAD --oneline` show what it would drop.

To keep the old pane instead of handing over, pass `--keep-source` (or use `launch` directly).

## Optional `memory_context`

Packages may carry an optional, additive `memory_context` block (Herdr Unified
Hindsight Memory, CONTRACT.md). It is produced by `herdr-handoff` only when a
`herdr-memory` CLI is available and configured; otherwise the field is absent.

```json
{"version": 1,
 "refs": [{"memory_id": "mem_1", "bank_id": "bank_a", "revision": "r3"}],
 "snapshot_digest": "sha256:<64 hex>",
 "checkpoint": {"state": "synced|pending", "outbox_pending": 0, "at": "2026-11-02T10:00:00Z"}}
```

- Identifiers only. Memory text and secrets are never copied into a handoff.
- `checkpoint.state` is `pending` when the memory outbox has unsent items or
  `herdr-memory status` was unreachable at handoff time. Offline outbox
  handoffs still queue and sync normally.
- Control plane (`POST /api/v1/handoffs`): packages without the block are
  accepted unchanged, and unknown top-level fields are tolerated as before. When
  the block is present it is validated strictly (see
  `schemas/handoff-memory-context-v1.schema.json`); a malformed block is
  rejected with HTTP 400 and nothing is stored, so a bad block can never
  half-apply. A valid block is returned by `GET /api/v1/handoffs[/<id>]` and
  included in the paged handoff summaries; lineage rows (Pi) pass a valid block
  through and drop a malformed one.
- Receiving side: `herdr-handoff resume` prints the refs under an
  "UNTRUSTED MEMORY REFERENCES" heading and re-resolves each through
  `herdr-memory get MEMORY_ID --scope S` (the scope is derived from the ref's bank id). Unresolved or forbidden refs are reported and never fatal.
  Retrieved memory is data, never instructions.
- Pi Blackhole short-term context handling is unchanged.

## Opt-out workflow hook: a `/handoff` reminder before compaction

`herdr-eng workflow install` (also `scripts/install.sh`, step 7c) registers a
Claude Code `PreCompact` hook. It **only prompts**: it prints a message
recommending `/handoff` (a new Herdr pane, same harness) before the context is
compacted. It does not run `herdr-handoff`, create or sync a package, open a pane,
or publish anything. The automatic context trigger below exists but is not
installed by default; the decision to hand off stays with the person unless they
opt in. Remove the hook with
`herdr-eng workflow uninstall`. Details: `docs/operations/workflow-hooks.md`.

## Opt-in automatic trigger: hand off when the context fills up

`herdr-eng handoff context-hook claude` is a Claude Code `UserPromptSubmit` hook
body. It is **not installed by default**. When the session's context usage reaches
the threshold it injects an instruction (`hookSpecificOutput.additionalContext`)
telling the agent to finish or record running subagents and run the `/handoff`
skill now, which hands over to a successor pane and closes this one. The hook
cannot write the handoff itself; it only injects the instruction. Each `session_id` has a
marker file recording the context size (tokens) at the last reminder; an ignored instruction is
repeated only after usage grows another 10 points. Markers older than 30 days are
pruned (marker files under `$HERDR_ENGINEERING_STATE/handoff-context`,
default `~/.local/share/herdr-engineering/state/handoff-context`).

Context used is read from the last assistant message in the transcript:
`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`. Below the
threshold, with a missing transcript, bad input or any error, it prints nothing and
exits 0, so it never blocks a prompt.

| Variable | Default | Meaning |
|---|---|---|
| `HERDR_HANDOFF_CONTEXT_THRESHOLD` | `0.50` | fraction of the window that triggers the instruction |
| `HERDR_HANDOFF_CONTEXT_MAX_TOKENS` | `200000`, or `1000000` once usage exceeds 200000 | context window size; setting the variable disables the inference |
| `HERDR_HANDOFF_AUTO` | unset | `0` disables the hook |

The transcript does not name the context window, so the hook assumes 200000 tokens and
switches to 1000000 once usage proves a 200000 window cannot be right. A 1M-token
session below 200000 tokens is therefore measured against 200000 and is reminded
early; set `HERDR_HANDOFF_CONTEXT_MAX_TOKENS=1000000` to measure it exactly.

To opt in, run:

```bash
herdr-eng handoff auto install     # idempotent; also: uninstall | check [--dry-run] [--json]
herdr-eng handoff auto off|on [--pane ID]   # per-pane switch for automatic handoff (default pane: $HERDR_PANE_ID)
```

`install` appends one `UserPromptSubmit` entry (plus the `SubagentStart` and `SubagentStop` entries described in the next section), each carrying the marker `herdr-handoff-auto`
to `~/.claude/settings.json`, keeping every other hook and key, and writes atomically.
An entry already present with that marker (even one you added by hand) is left as is
(`unchanged`, or `preserved` if its command differs), so it never duplicates; the result has one item per event, and a settings file that only has the older `UserPromptSubmit` entry gets the two new ones added. If
`settings.json` is not valid JSON the command reports `refused` and does not touch the
file. `uninstall` removes only hooks carrying the marker. `check` reports `ok` or
`missing` per event (exit 1 if any of the three is missing).

Manual alternative: add this to `~/.claude/settings.json` yourself:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "[ -x \"$HOME/.local/bin/herdr-eng\" ] && \"$HOME/.local/bin/herdr-eng\" handoff context-hook claude || true # herdr-handoff-auto"
          }
        ]
      }
    ]
  }
}
```

## Running-subagent registry

`herdr-handoff takeover` must not close a pane that still has running subagents. To make that knowable,
`herdr-eng handoff subagent-hook start|stop` is the body of the Claude Code `SubagentStart` / `SubagentStop`
hooks. It reads the hook JSON from stdin (`agent_id`, `agent_type`) and keeps one file per running subagent:

```
$HERDR_ENGINEERING_STATE/subagents/<pane_id>/<agent_id>      # default state root: ~/.local/share/herdr-engineering/state
{"agent_id": "...", "agent_type": "...", "started": <unix seconds>}
```

`<pane_id>` is `$HERDR_PANE_ID` of the hook process (the hook does nothing when it is unset). Both ids are
sanitised to `[A-Za-z0-9_.:-]` (anything else becomes `_`), so a hostile id such as `../x` cannot leave the
directory. `start` writes the file atomically; `stop` removes it (a missing file is fine). The hook always exits 0
and prints nothing, even on garbage input or an unwritable state directory.

Inspect the registry with `herdr-eng handoff subagents [--pane ID] [--json]` (default pane `$HERDR_PANE_ID`). It
lists the live entries and deletes entries older than 6 hours (a subagent that never reported a stop) as stale. It
always exits 0.

Three mutually exclusive flags make the registry machine-readable for a controller; each needs a pane (`--pane ID`,
else `$HERDR_PANE_ID`, else an argument error with exit 2):

- `--status` prints `{"known": bool, "live": int}` with `--json` (else `known=true live=1`). `known` is false when no
  hook ever fired for the pane, and true once its directory exists, even with zero live agents. Stale entries are not
  counted.
- `--wait SECONDS` polls until no subagent is live: exit 0 when idle (an unknown pane counts as idle; the caller decides
  what unknown means), exit 3 on timeout. With `--json` it prints the status it last saw.
- `--purge` deletes the pane's registry directory and prints the number of entries removed (`{"purged": N}` with
  `--json`); run it when a pane closes so a reused pane id starts clean.

Without these flags the command behaves as described above.

`herdr-eng handoff context [--pane ID] [--json]` prints how full the pane's session context is (harness, used and
window tokens, percent, source, window source), or `{"unknown": true}` when it cannot be read; it always exits 0.
`--harness NAME --session-id ID [--cwd DIR]` reads a named session's record directly, without Herdr.

`herdr-eng handoff auto install` installs these two hooks together with the `UserPromptSubmit` one. Manual
alternative, merged into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SubagentStart": [
      {"hooks": [{"type": "command",
        "command": "[ -x \"$HOME/.local/bin/herdr-eng\" ] && \"$HOME/.local/bin/herdr-eng\" handoff subagent-hook start || true # herdr-handoff-auto"}]}
    ],
    "SubagentStop": [
      {"hooks": [{"type": "command",
        "command": "[ -x \"$HOME/.local/bin/herdr-eng\" ] && \"$HOME/.local/bin/herdr-eng\" handoff subagent-hook stop || true # herdr-handoff-auto"}]}
    ]
  }
}
```

## Automatic handoff controller

The controller watches every agent session that Herdr reports (Claude Code, Codex, Pi and OpenCode), and when a session's
context fills up it asks the session to write a briefing, then hands the work to a fresh session of the same harness in a
pane below it. It is on by default for every session. It is one process, `herdr-eng handoff controller run`, which looks at
all sessions once every 5 seconds (`--interval` changes that; `--once` runs one tick and exits). It skips the pane it runs
in and any side pane, and it only acts on an agent that Herdr names `claude`, `codex`, `pi` or `opencode`.

This replaces the older per-harness automatic triggers. Those policies (the opt-in `context-hook`, and any Pi or
OpenCode equivalents) are retired by plan 5 in the same release and must not run alongside the controller: two things
typing a handoff order into one session would hand it off twice. Until plan 5 ships, do not run the controller on a
host that has one of them installed.

### When it acts

The controller reads each session's context use from the session's own record. The limit is 80 percent of the
context window; the hard ceiling is 12 points above it (92 percent); the drain timeout is 30 minutes and the briefing
timeout is 5 minutes. A session with no usage yet is skipped; a session whose window is unknown is not skipped (the adapter
falls back to a known window). Between the limit and the ceiling the controller tries a normal handoff. Above the
ceiling the context is too full to wait for long, but a session that is busy is not given up on: the controller waits for
it, tick by tick, up to the drain timeout (30 minutes from the trigger), and types the order as soon as the pane is ready.
Only when that bound passes without the order typed, or when the pane was ready and typing still failed, does the
controller take the last resort (see "Last resort").

### The steps, in order

1. `handoff.triggered`: the session reached the limit.
2. The drain order is typed into the session (next section). `handoff.drain_started` is written just before the first
   attempt, so a restarted controller never types it twice. If the order could not be typed within 30 minutes, the chain
   ends red (`drain_timeout`, `blocked` and so on, below); over the ceiling it takes the last resort instead.
3. The session carries out the order: starts no new work, waits for its subagents, commits and pushes to the feature branch,
   and writes the briefing with `herdr-handoff create`. The controller looks for the briefing once per tick, for up to
   5 minutes after the order was typed. `handoff.drained` and `handoff.briefing_written` follow. The controller then
   keeps looking while the session works: the fallback writer (step 4) runs only after the session has been quiet (idle,
   no live subagents) for 5 minutes, or 30 minutes after the trigger, and never sooner than 5 minutes after the order was
   typed. Over the ceiling the quiet time is not waited for: the fallback runs 5 minutes after the order was typed.
4. If no briefing appeared by then, the controller writes one itself with the fallback writer (below):
   `handoff.briefing_fallback`. If that cannot write one either, the chain ends red (`no_briefing`).
5. A checkpoint commit of tracked changes on the feature branch (`wip: auto-handoff checkpoint`), `handoff.wip_committed`.
   The controller never commits on main or master, and never on a detached head, and an unknown git state ends the chain red
   rather than guessing. If the status shows tracked changes but `git add -u` stages none (a dirty submodule), nothing is
   committed (`NOTHING_STAGEABLE`) and the takeover goes on with `--allow-dirty-source`.
6. `handoff.takeover_started`: `herdr-handoff takeover` launches the same harness in a pane below the source, waits for proof
   that it is live, asks the source session to exit, and closes the source pane. It is tried up to 3 times; a rerun for the
   same handoff reuses the recorded child pane and never launches a second one. Before every call to `herdr-handoff takeover` (the first,
   each retry, and one resumed after a restart) the controller reads the session afresh: idle or done, an empty input, the
   right agent and no live subagents. If it is not ready, no call is made and the chain keeps waiting (`handoff.waiting`) for
   up to 5 minutes after the briefing; after that it takes the last resort.
7. One terminal event ends the chain. `handoff.skipped` is not a step: it records why a session was left alone.

### What is typed, and when nothing is

The controller types into a session in three cases only: the drain order (once per chain), and, in the last resort,
the harness's compact command and then a short pointer to the briefing file. The source harness's exit command is typed by
`herdr-handoff takeover`, not by the controller, and only when Herdr reports an agent in the pane. The drain order is:

> Context is nearly full; hand off now. 1) Start no new work. 2) Wait for your subagents to finish. 3) Commit and push to
> the feature branch, never main. 4) Write the briefing with herdr-handoff create, target <harness>. 5) Stop.

(When the controller does not know whether subagents run, step 2 says to list the background agents first.) It is submitted
with `herdr agent prompt` (Herdr's own submission, bracketed paste), and only when Herdr reports the agent idle or done and
the screen shows an empty input, read twice in a row. Nothing is typed when the agent is working or blocked on a dialog,
when the input holds text (a person may be typing), when the pane runs another harness than the session's record, when the
order would exceed the 900-byte typed-input limit, when the switch is off, when the session was already handed off, or when
the controller has given up on the session. After a failed `herdr agent prompt` the controller never types the order
again, except in the one case below (a doubled order is worse than a late one): it waits to see whether the agent starts
working.

Retyping: at most one retype per chain, and only after an uncertain prompt (`prompt_uncertain`, where it is unknown whether
the order landed) and a fresh read showing a ready, empty input with the agent idle. Otherwise it waits, and a briefing that
appears ends the wait.

While an agent is still working, or has been quiet for less than the quiet period, the controller does nothing to it and
writes a `handoff.waiting` event only when the reason or the number of live subagents changes, so the log shows why a full
session was not drained. The reasons are `working` (the pane is busy, blocked on a dialog or has text in its input),
`subagents` (the session has live subagents; the count is on the event) and, before the takeover, the same two from the
ready check.

### The per-session switch

`herdr-eng handoff auto off` turns the controller off for one pane, and `herdr-eng handoff auto on` turns it back on
(`--pane ID`; the default is `$HERDR_PANE_ID`). The switch is a marker file under
`$HERDR_ENGINEERING_STATE/handoff-controller/off/` (default state root `~/.local/share/herdr-engineering/state`). Setting
`HERDR_HANDOFF_AUTO=0` in the controller's environment turns it off for every session. A session whose switch is off gets one
`handoff.skipped` event (`switch_off`). The controller reads the marker on every tick, so a toggle takes effect within
5 seconds. A toggle off and on again wholly between two ticks is seen too: the marker file stays after `on` (its mtime is the
time of the last toggle), and failures from before that time no longer count towards `gave_up`.

### The event log and the colours

Every step is one JSON line in `events.jsonl` in the shared handoff store (below). Each line has the event name, a UTC
time, the chain id, the pane and session, the harness, the handoff id and the predecessor and successor ids, a colour on
terminal events, a reason, the context percent, and (on events that name a successor) `successor_pane_id` and
`successor_session_id`. `handoff.wip_committed` also carries the checkpoint `commit` (its `reason` is `committed`, or
`push_not_allowed` or `push_failed` when the commit was not pushed). The twelve events are `handoff.triggered`, `handoff.drain_started`, `handoff.drained`,
`handoff.briefing_written`, `handoff.briefing_fallback`, `handoff.wip_committed`, `handoff.takeover_started`,
`handoff.succeeded`, `handoff.failed_compacted`, `handoff.stuck`, `handoff.skipped` and `handoff.waiting`. A second
`handoff.takeover_started` with reason `compacting` marks the start of the compaction fallback: a controller restarted after
it does not try the takeover again. The last terminal event of a chain
decides its colour:

| Colour | Event | Meaning |
|---|---|---|
| green | `handoff.succeeded` | handed off: the successor is live and the source pane is closed |
| orange | `handoff.failed_compacted` | the handoff could not be completed; the source session compacted and continues, pointed at the briefing; `reason` says why |
| red | `handoff.stuck` | the controller stopped and left the session as it was; `reason` is one of the values below |

`herdr-eng handoff controller status` prints whether a controller runs (it holds the lock) and the last event of every chain
that has not ended.

### Terminal outcomes and what to do

The controller never closes a pane itself; only `herdr-handoff takeover` does, after the successor is proven live. On every
red or orange outcome the source session keeps running.

| `reason` of `handoff.stuck` | Meaning | What to do |
|---|---|---|
| `drain_timeout` | the session was never idle with an empty input for 30 minutes | finish or interrupt its work, then run `/handoff` yourself |
| `blocked` | the session was waiting on a dialog when the 30 minutes ran out (the controller tries again every tick until then) | answer the dialog, then run `/handoff` or wait for the next chain |
| `wrong_agent` | the pane runs another harness than the session's record | check the pane; no action is taken |
| `drain_prompt_uncertain` | typing the order failed in a way that leaves it unknown whether it landed | look at the pane; run `/handoff` if it did not start |
| `drain_order_too_long` | the order exceeds the typed-input limit | report it; use `/handoff` |
| `no_record` | the session's record stayed unreadable for 30 minutes mid-chain | check that the harness still writes its session file |
| `no_briefing` | no briefing, and the fallback writer could not write one (OpenCode, or the headless call failed) | run `/handoff` yourself |
| `git_state_unknown` | a git command failed or timed out, so the state of the work is unknown | inspect the repository (for example a leftover `.git/index.lock`, which the controller never deletes) |
| `record_unreadable` | `herdr-handoff inspect` could not read the record | check the record with `herdr-handoff inspect`; run `/handoff` yourself |
| `pane_gone` | the pane of an open chain was not listed, or held another session, for 60 seconds of credited time (`pane_gone_after_s`) | look at what replaced it; start a handoff by hand if work remains. It does not count towards `gave_up` |
| `compaction_state_unknown` | a controller restarted while a compaction had begun and no end was recorded | look at the pane; the controller never types `/compact` a second time |
| `merge_in_progress` | a merge, cherry-pick, revert or rebase is unfinished, so nothing was committed | finish or abort it. Where pushing is not allowed the controller goes on without committing (`--allow-dirty-source`); where it is allowed the chain stops |
| `on_main_with_changes` | the session is on main, master or a detached head with uncommitted or unpushed work | move the work to a feature branch; the controller will not commit on main |
| `launch_state_unknown` | a takeover died while placing the successor, a child may or may not exist | look for a pane below the source before doing anything; the controller will not retry |
| `successor_without_takeover` | the record names a successor but no launched child | inspect the record with `herdr-handoff inspect`; do not launch another |
| `source_not_closed_successor_live` | the successor is live; only closing the source failed | close the source pane yourself; the controller marks it handed off and never drains it again |
| `no_compact_command`, `compaction_not_ready`, `compaction_not_started`, `compaction_timeout`, `pointer_too_long` | the handoff failed and compaction, the last resort, could not run either | compact or hand off by hand; the briefing is in the store |
| `takeover_refused_and_compaction_failed` | takeover was refused three times and compaction failed | same; the `reason` text carries herdr-handoff's last refusal |
| `internal_error:<Type>` | finishing the chain raised three times in a row | the controller has a bug; the session is untouched |

`handoff.skipped` carries `switch_off`, `no_session` (the record could not be found), `no_record` (no usage yet) or
`gave_up` (below), once per pane and reason in a row.

### Retries and `gave_up`

An orange or red chain may start again 30 minutes after it ended, then 60 minutes after the next one ended, and the
third non-green end is final: `gave_up`. The controller writes one `handoff.skipped` with reason `gave_up`, only at a moment
when it would otherwise have started a drain, and once per session. It then leaves the session alone until its session id
changes or its switch is turned off and on. A session that would have succeeded on a fourth try is therefore left to the
harness's own compaction. The waits count credited time (see below), so a laptop asleep for hours does not use them up.

### Last resort: compaction

Compaction is used only in the fallback path: over the ceiling once the 30-minute bound passed without the order typed (or
a ready pane could not be typed), or after takeover failed 3 times without a confirmed successor, or when the 5-minute wait for a ready pane before the takeover ran out. The controller types the harness's compact command and then a pointer to the briefing file, each only
into a ready pane (idle, empty input); it may do so with live subagents. This is the orange
outcome. If the successor is confirmed live but the source would not close, the controller does not compact (two sessions on
one task is worse than one stale pane): that is red `source_not_closed_successor_live`.

A controller restarted after the compaction began, with no end recorded, does not type again: the chain ends red
(`compaction_state_unknown`).

The whole compaction path is bounded at 735 seconds: 60 s for the compact command to become ready, 15 s to see it start,
600 s for it to finish, 60 s for the pointer to be accepted, plus the short delays between typed keys. Past that the chain
ends red (`compaction_timeout` and its relatives).

### Pushing

The controller never pushes unless the session's repository is listed in `push_allowed_repos`, set with
`HERDR_HANDOFF_PUSH_REPOS` (directories separated by `:`; a worktree counts when it, or the main checkout of its repository,
is under one of them). `HERDR_HANDOFF_PUSH_DEFAULT=1` allows it for every other repository. Without it the briefing records
the unpushed commits. Repositories whose own rules forbid pushing (herdr-engineering itself) must not be listed.

### The fallback writer

When the session does not write the briefing in time, the controller writes it with one headless model call to the same
harness the session uses (the same provider; the transcript tail goes nowhere else). The call runs in a fresh empty
directory under `<store>/fallback-work/`, with every `HERDR_*` and session-identity variable removed, and with every tool
turned off: Claude (`--tools ""`, `--setting-sources ""`, `--safe-mode` when offered), Pi (`--no-tools` and the other `--no-*`
flags), and Codex with a read-only sandbox in the empty directory (`--ignore-user-config --ignore-rules` when offered).
OpenCode has no way to turn tools off from the command line, so it is not enabled: its fallback briefing is never written
and it compacts instead (red `no_briefing` when that fails). The record is created with `herdr-handoff create --fallback`,
which stores `provenance.fallback = true`, so a briefing that the session did not write is always marked.

### The one-controller lock

One controller runs per host. It holds an exclusive `flock` on
`<state root>/handoff-controller/controller.lock` for as long as it lives; the kernel drops the lock when it dies, so there
is no stale pid. A second controller prints a message and exits 0.

### Required programs and the shared store

The controller needs `herdr`, `herdr-handoff` (0.5.0 or newer) and `git`, and uses `gh` when present (the fallback writer lists
open pull requests). It resolves them to absolute paths once, at start, from `PATH` plus the directories in
`HERDR_HANDOFF_BIN_DIRS` (`:`-separated; useful when a plugin's `PATH` is short); a missing required one is a one-line error
and exit code 2. The same directories are used to find `claude`, `codex` and `pi` for the fallback writer.

An error on the file system during a tick (for example a full disk or a permission problem on the state directory) is
logged as one line naming the exception type only. The loop waits 5 seconds, doubling to 60 seconds, and tries again; with
`--once` the exit code is 1.

Timeouts inside the controller use the monotonic clock. Wall time is stored only to survive a restart. After a restart an
event's age is its wall-clock age; a tick gap of more than 3 intervals in a running process (a sleeping machine) credits at
most one interval. Any exception a tick raises is logged by its type and retried with the same back-off; a lock held by
another controller is retried for about a second before this one gives up.

All handoffs from all repositories go to one store: `$HERDR_HANDOFF_STORE`, default
`~/.local/share/herdr-handoff/store`. Records from the old per-repo stores (`<repo>/.herdr/handoffs`) are copied in, once
and without changing or deleting the originals, with `python3 scripts/handoff_import_legacy.py [REPO ...]` (the current
repository by default); running it again imports nothing.

### Installation

The plugin manifest is `deploy/herdr-handoff-controller-plugin/herdr-plugin.toml` (a `[[startup]]` command that runs the
controller, and a status action). Herdr 0.9.1 accepted it with `herdr plugin link` on an isolated test server. `scripts/install.sh`
has no step for it yet: linking it is added to the installer in a later change. Until then start the controller yourself
with `herdr-eng handoff controller run`.

### Known limits

* Verified only by a live run (`tests/e2e/test_handoff_controller_live.py`, not yet run): the headless flags of each harness
  (they come from each CLI's `--help`, no real model call was made), that `herdr agent prompt` submits correctly on Codex
  (Codex dropped an Enter sent together with text), and the names Herdr gives Codex and OpenCode agents (`codex` and
  `opencode` are assumed; `claude` and `pi` were measured on Herdr 0.9.1). A wrong name means the controller does not manage
  that harness at all.
* `events.jsonl` is not rotated, and the `off/<pane>` markers of the switch accumulate. Both are small; clean them by hand
  if they grow. A poller reads new events with `EventLog.read_since`, which also notices a replaced file.
* An open chain whose pane is gone, or whose session changed, ends with one `handoff.stuck` reason `pane_gone`, after 60
  seconds of credited absence.
* Between the controller's last check and `herdr-handoff` typing the exit text (the successor split, launch and confirmation:
  up to about 25 seconds) a session that starts a turn, or a user who starts typing, is not protected. `herdr-handoff`
  types the exit text only when Herdr reports an agent, not when it is idle. The follow-up is a herdr-handoff 0.5.1 change.
* The fallback writer runs inside the loop and can block every session's tick for up to 5 minutes.
* A takeover killed after the child was launched but before it was confirmed counts as unconfirmed, so the last-resort
  compaction can run while a slow child is still starting; the `successor_pane_id` on the orange event shows it.
* An agent that turns `blocked` right after a failed prompt is reported as typed; the screen cannot say whether the text went in.
* Tests that need Herdr (`herdr_server`, `herdr_live`) skip in CI, so CI does not cover those paths; the decision logic is
  unit-tested without Herdr.
