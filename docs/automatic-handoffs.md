# Automatic handoffs

A handoff is not something the operator has to ask for. When a session gets close to its
context limit, the machine hands it to a fresh pane by itself — before the harness's own
summarizer replaces the conversation with a lossy recap.

```bash
scripts/install.sh                       # arms it (step 7e); re-running is idempotent
herdr-eng handoff autopilot status       # policy, what is installed, what has fired
herdr-eng handoff autopilot install      # arm it on a machine that was installed earlier
herdr-eng handoff autopilot uninstall    # remove both triggers; the audit ledger is kept
herdr-eng doctor                         # handoff-autopilot: ok | warn | skip | fail
```

Installing is safe on a machine with existing hooks: Claude Code entries are written only
inside groups the autopilot wrote itself, so foreign hooks in `~/.claude/settings.json` are
kept, and `uninstall` removes only what it added.

## What is not automatic

Publishing is not automatic. A trigger tells the agent to run the handoff skill; the skill
creates a private handoff, opens a pane, and takes over a branch and a worktree. Nothing here
shares an artifact, publishes a report, opens a pull request against a protected branch, or
touches `main`. Those stay human-approved publication actions, exactly as they were before the
triggers existed — see [Handoffs](operations/handoff.md).

Two other triggers write to the same `~/.claude/settings.json`, and they are separate installs:

| Trigger | Installed by | Fires on | What it does |
|---|---|---|---|
| `handoff autopilot install` (this page) | `install.sh`, default on | Pi `turn_end`; Claude `Stop`, `PreCompact` | tells the agent to run the handoff skill, once per session |
| `workflow install` | `install.sh`, default on | Claude `SessionStart`, `PreCompact` | reminds the agent of the engineering defaults, and of `/handoff` before compaction; starts nothing |
| `handoff context-hook claude` | never; paste the block into `~/.claude/settings.json` | Claude `UserPromptSubmit` | advises at half the window (default), once per session |

Each installer owns its own marker, so installing one never rewrites or removes another's hook
group, and `install.sh` runs the workflow hooks (7c) before the autopilot (7e). A session warned
by the opt-in `context-hook` is not told again by the autopilot: `decide()` reads that trigger's
`$HERDR_ENGINEERING_STATE/handoff-context/<session>.fired` marker before spending its own
per-session budget. The reverse is not true — the `context-hook` does not read the autopilot's
ledger, so a session already told at the threshold can still get the earlier advice on its next
prompt. `docs/operations/workflow-hooks.md` covers the workflow reminder.

## What is installed

| Piece | Where | Fires at |
| --- | --- | --- |
| Pi extension | `~/.pi/agent/extensions/herdr-handoff-autopilot.ts` | `turn_end`, read through `ctx.getContextUsage()` |
| Claude Code hooks | `~/.claude/settings.json` → `hooks.Stop`, `hooks.PreCompact` | end of each agent turn; pre-summarize |
| Policy + ledger | `~/.config/herdr-engineering/config.yaml`, `$XDG_STATE_HOME/herdr-engineering/handoff-autopilot.json` | consulted by both of the above |

Both triggers ask the same CLI question and do what the answer says:

```bash
herdr-eng handoff autopilot check --session-id <id> --harness pi --percent 84.7 --json
```

```json
{
  "trigger": true,
  "code": "context_is_full",
  "threshold": 50.0,
  "percent": 84.7,
  "prompt": "[HerdR automatic handoff] Context is at 85% of the window …hand off now, without asking me first…"
}
```

The extension carries **no thresholds**. `check` is read-only; `record` is what consumes a
trigger, and it runs only after the instruction was actually delivered. Codex and OpenCode
have no installed trigger yet — `check` answers `harness_unsupported` and says so rather
than inventing a boundary they do not provide.

## Why the trigger is the turn boundary, not compaction

Pi compacts **inside the same session** (`pi-coding-agent/dist/core/compact.js`:
`checkCompaction()` then `compact()` in the live agent session). By the time that runs the
detail is already gone and there is no successor pane to take over, so compaction is the wrong
place to hand off. `turn_end` with a full context is the last boundary that still holds the
whole conversation — the next turn is the one that would be summarized. Claude Code's `Stop`
hook is the equivalent boundary; its `PreCompact` hook fires earlier but, verified against the
installed 2.1.306 binary, cannot deliver `additionalContext` to the model, so there the
autopilot records the trigger and shows the operator a `systemMessage` instead of pretending
the instruction landed.

## Policy

```yaml
handoff_autopilot:
  enabled: true             # false = back to handoffs-on-request-only
  context_percent: 50       # matches the agent rule "warn past 50% of context"
  max_per_session: 1        # one instruction per session; 0 is refused (it would disable it silently)
  harnesses: [pi, claude]   # what the triggers are allowed to act on
  context_window_tokens: 200000   # what to assume when a harness reports usage but no window size
  large_context_window_tokens: 1000000   # assumed instead once usage proves 200k impossible
  model_context_windows:          # window by model name (substring, longest wins); merged with the default
    "[1m]": 1000000               # built in: a `[1m]` model has a 1M window
    sonnet-big: 500000            # add your own
```

Read from the machine config (`~/.config/herdr-engineering/config.yaml`) or
`$HERDR_ENGINEERING_CONFIG` only. Repository-local config files are untrusted, so a
`handoff_autopilot` section in `./config/herdr-engineering.yaml` or `./herdr-engineering.yaml` is
ignored; later of the two readable sources wins.

**The window, and why it is sometimes an assumption.** Pi states both numbers, so the percentage
is exact. Claude Code's transcripts record `message.usage` and the model, but never the size of
the window in use, so `context_window_tokens` is the assumed denominator - and an assumption on a
1M-context model would fire the trigger at 100k tokens, one fifth of the way in. Three rules keep
that from happening:

- the window is read from the model the transcript names: `model_context_windows` (YAML only, no
  environment variable) maps a model-name substring to its window, and a `[1m]` model is 1M out of
  the box. The answer reports `"source": "model"` and the model it matched.
- usage beyond the assumed window proves the assumption wrong, so the measurement moves to
  `large_context_window_tokens` (this mirrors the 1M switch the opt-in
  `handoff context-hook claude` already makes). A session at 250k is therefore 25%, not 125% -
  and it is still a trigger candidate rather than an unusable reading.
- declaring the real window ends the guessing: put `context_window_tokens: 1000000` in the config
  and every percentage is computed against it.

Every answer says which of the two happened - `"window": {"declared": true, "source": "config"}`
or `{"declared": false, "source": "assumed", "how_to_declare": ...}` - so a handoff that looks
early can be read as a wrong assumption instead of a threshold bug. `handoff autopilot check
--transcript <path>` reports the same window as the Stop hook, because both take tokens from the
transcript and divide by the window rule, never a percentage the harness asserted.

**Switch it off without uninstalling:**

```bash
export HERDR_HANDOFF_AUTOPILOT=off       # this shell, and any session started from it
```

```yaml
# ~/.config/herdr-engineering/config.yaml  — machine-wide
handoff_autopilot:
  enabled: false
```

`status` and `doctor` name whichever switch is holding it off, and how to put it back.

## What the agent is told

The instruction is injected as the next user turn, silently
(`sendUserMessage(prompt, { deliver: false, inject: false })` in Pi;
`hookSpecificOutput.additionalContext` in Claude Code). It says: run the `handoff` skill now,
do not ask, do not finish the current task first, commit and push before the takeover — and if
the tool refuses to close the source pane because of uncommitted or unpushed work, stop and
report it rather than forcing the close. The trigger never calls the handoff tool itself: the
skill is the only component that knows how to prove a takeover, and the CLI still refuses on
`launch_unconfirmed`, `launch_failed`, `source_close_failed` and `source_dirty` (whose reason
names whether the work is uncommitted, unpushed, or unknown to git).

## Failure modes

| Situation | Behaviour |
| --- | --- |
| `herdr-eng` missing, older than the extension, or it answers with nothing | the session is left alone; one UI notice per session, never a per-turn error |
| Pi reports `percent: null` (no tokens, or no window) | no handoff on an unknown context size (`unknown_context`) |
| Claude transcript unreadable / no usage record | the hook prints nothing and exits 0 |
| Trigger ledger corrupt | triggers stay armed; `status` and `doctor` warn and name the file |
| Injection refused by the harness | nothing is recorded, so the next boundary re-arms the trigger |
| A handoff for this session is already `draft`/`queued`/`claimed`/`accepted`/`in_progress` | `handoff_underway` — no second instruction |
| `herdr-handoff confirm` answered `launch_unconfirmed` | the close was refused and both panes stay up — nothing was destroyed; report it, because re-running does not make an unobserved successor live |

**Where it deliberately does not act:** mid-turn (only turn boundaries), a second time in the
same session, on a harness policy does not name, on `SessionEnd`/`PreToolUse`/`PostToolUse`
(wrong moment or fires many times a turn), or on a pane the successor did not prove.

Failing open is deliberate: a broken autopilot must never be able to stall the session it was
installed to protect. Every `handoff autopilot hook` path exits 0.

## Cost

Pi awaits extension handlers in stream order, so a subprocess on every turn would slow every
turn. The extension caches the threshold **the CLI reported** and stays quiet while the
context is nowhere near it, re-asking every 20 evaluations so a mid-session config change is
obeyed; a raised threshold is honoured immediately because the authoritative `check` still runs
whenever the cached threshold is crossed.

## Audit

```bash
herdr-eng handoff autopilot status --json      # policy + installed state + per-session ledger
cat "${XDG_STATE_HOME:-$HOME/.local/state}"/herdr-engineering/handoff-autopilot.json
```

The ledger records, per session: how many times it was told to hand off, when, on which
harness, the percent that triggered it, the boundary that noticed, and a hash of the
instruction. `handoff autopilot uninstall` removes the extension and the hooks but keeps this
file — it is the audit record of when the machine took over on its own.

Only `XDG_STATE_HOME` moves that file. `HERDR_ENGINEERING_STATE` sounds like the same knob and is
not: it relocates the *other* trigger's marker directory
(`$HERDR_ENGINEERING_STATE/handoff-context/<session>.fired`), and the ledger keeps resolving
through `XDG_STATE_HOME`. A probe that redirects `HERDR_ENGINEERING_STATE` to a scratch directory,
expecting to sandbox itself, spends a real trigger against a session that never happened — and
`uninstall` deliberately keeps whatever it writes. Sandbox with `XDG_STATE_HOME`:

```bash
XDG_STATE_HOME="$PWD/.tmp/autopilot-probe" herdr-eng handoff autopilot status --json
```

`install` rewrites `~/.claude/settings.json` in place through a temp file in the same directory,
preserving the file's existing mode, its unrelated keys and every foreign hook group, but it takes
no backup of its own; copy the file first if it holds anything you cannot reconstruct.

## Checking the trigger itself

```bash
npm run typecheck:handoff-autopilot   # the Pi trigger, checked against the Pi API it targets
node --test packages/herdr-handoff-autopilot.test.js
```

Pi's extension API has changed field names between builds (`ContextUsage`, how a message is
injected silently), so the extension ships with its own `tsconfig.json` and is also driven by
a recording Pi host in the Node suite — the test imports the file that installs, not a copy.

The `record` half of every path is explicit: Pi calls `record` only after the injection
returned, and a Claude hook records only when its command carries `--record` — which is what
`handoff autopilot install` writes, but a hand-written hook without the flag reports without
consuming. Nothing consumes a trigger on a path where delivery was not proven.
