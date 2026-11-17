---
name: handoff
description: Hand the current session off to a fresh agent in a new Herdr pane, defaulting to the same harness. Builds a self-sufficient, evidence-based briefing (goal, done, remaining, decisions, risks, validation, resume point, repo/branch/PRs, next commands, constraints) and hands the pane over to the target, closing the replaced pane only once the successor is proven live. Use when asked to hand off, continue in a fresh session, or move work to claude, pi, codex or opencode.
---

# Session handoff

Usage: `/handoff [pi|claude|codex|opencode]`. With no argument the target is the **same harness** as this session.

This skill composes the briefing and then reuses the `herdr-handoff` CLI (`create`, then `takeover`); it does not fork it. It is the *default-target, history-grounded* form of the explicit `/herdr::toClaude`, `/herdr::toPi`, `/herdr::toCodex` and `/herdr::toOpenCode` commands, which use the same two CLI calls. Pi's built-in `/handoff` is a different, planner-worker feature: this skill does not replace it. For a read-only narrative of the session, use `/summary`.

Remain otherwise read-only: never modify files, commit, push, build, deploy or restart anything. The only writes are the handoff files `herdr-handoff` makes under its own outbox, the new pane it opens, and the close of the pane it replaces (which happens only after the successor is proven live, and never while that pane still holds work).

## 1. Pick the target

1. If the user named one of `pi`, `claude`, `codex`, `opencode`, that is the target. Anything else: stop and ask them to name one of the four.
2. Otherwise run `herdr-eng handoff harness`. Exit 0 prints `{"harness": ...}`: use it. Exit 2 means the current harness could not be established reliably: **stop and ask the user to name the target**. Never guess from the newest session file.
3. The source harness is the one from step 2 (when the user named a different target, run `herdr-eng handoff harness` anyway and use its answer as source, or omit `--source-harness` if it fails).

## 2. Gather evidence (read-only)

1. `herdr-eng summary history --json` gives normalized, redacted events, the herdr journal projection and a `coverage` verdict with `gaps`. Use it as the conversation record. If it is missing or fails, fall back to the conversation visible to you and say coverage is partial.
2. Git facts, always as `git --no-optional-locks`: `rev-parse --show-toplevel`, `branch --show-current`, `status --short`, `log -n 10 --oneline`, `diff --stat`. Use the helper's `cwd` when it differs from the current directory.
3. Open pull requests for the branch: `gh pr list --head <branch> --json url,number,state` when `gh` is available. Only record URLs you actually saw.
4. A published `/summary` report for this session, if any, is the full narrative; reference its absolute path, do not paste it.

## 3. Compose the briefing

Build one JSON object. Every item must be traceable to the history, a tool result or git output.

- `goal` (required): the user's current objective in one or two sentences.
- `completed`, `remaining`, `decisions` (with the reason), `risks`, `validation` (exact command and result, with when it last ran).
- `constraints`: user rules and standing instructions that still bind (for example "never commit to main", "do not deploy"). Copy them faithfully.
- `next_commands`: exact commands to run first, each runnable as written from the worktree.
- `files`: absolute paths of the key files.
- `resume_point`: one to three sentences saying exactly where to continue.
- `workspace`: `{"repo": "owner/name", "branch": "...", "worktree": "/abs/path"}`.
- `pull_requests`: https URLs only.
- `unverified`: everything the agent claimed but no tool result confirms, plus every coverage gap.
- `report`: `{"path": "/abs/path/to/published-summary.md"}` when a published summary exists.

Rules: never fabricate paths, hashes, URLs, counts or results; mark doubt in `unverified` rather than leaving it out; never include credentials, tokens, private keys, hidden reasoning or raw transcripts (`herdr-handoff` also redacts known secret shapes, but do not rely on it). The briefing must be enough for an agent that cannot see this conversation.

## 4. Create and hand over

1. Check the CLI: `herdr-handoff --version` must print `0.4.0` or newer (older versions lack the flags below, and pre-0.3.0 cannot close a replaced pane at all). If it is missing or older, stop and tell the user to update herdr-handoff; do not fall back to a thinner briefing silently.
2. Create, passing the JSON on stdin through a quoted heredoc:
   `herdr-handoff create --goal "<goal>" --source-harness <source> --target-harness <target> --from-json - <<'HANDOFF_EOF'` ... `HANDOFF_EOF`
   Add `--model <id>` only if the user asked for a model. The command prints the queued envelope; take `handoff_id` from it. A validation error means fix the briefing and retry.
3. Check for running work first. Closing this pane ends this session and every subagent or background task it started, and `takeover` cannot see them. Before running `herdr-handoff takeover`, wait for subagents and background tasks to finish and record their results in the briefing, or list what is still running under `remaining`. If something is still running and cannot be waited for, hand over with `--keep-source` so it is not killed, and tell the user the old pane is still open.
4. Make the source worktree durable, then hand over: commit and push what matters, from the worktree `git -C <worktree>` resolves to (for a linked worktree that is its own path, not the upstream checkout), and only then run `herdr-handoff takeover <handoff_id> --cwd <worktree>`.
   - `status: interactive_started`: the target is running in this tab (a split of this pane, else a new tab or workspace) with a prompt to read the handoff document first, and this pane is closing. Report only what the result actually reports; never claim the old session was closed when it was not.
   - `status: launch_unconfirmed` / `launch_failed`: the close was refused, so BOTH panes stay up - nothing was destroyed. Report it; re-running does not make an unobserved successor live, and forcing it is not the fix.
   - `status: source_dirty`: this pane's worktree holds work that is not durable, so the close was refused — the reason names which: `worktree_dirty` (tracked changes uncommitted), `worktree_unpushed` (commits no remote branch has) or `worktree_unknown` (git could not be consulted, which refuses too). The successor is live and the handoff succeeded; only the close did not. Make the work durable (commit, push), then run `herdr-handoff takeover <handoff_id>` again with the **same id**: it reuses the successor already recorded for that handoff rather than starting a second pane, and closes this one now that git is clean.
   - `status: source_close_failed`: the handoff went ahead and herdr refused the close - report it, do not force it.
   - `status: manual_launch_required`: herdr is not usable here. Show the user the printed `command` verbatim, run from `cwd`; `resume_command` prints the full resume prompt.
   - Keep the source pane up (the pre-0.4.0 behaviour) with `--keep-source`.
   - `--allow-dirty-source` closes a pane with uncommitted or unpushed work and loses it. Use it only when the user says to; `git -C <worktree> status --short` and `git -C <worktree> log @{u}..HEAD --oneline` show what would be lost. It cannot bypass the live-successor or same-tab checks, and it is never a retry of a refusal.
   - Never close a pane by hand to make a refusal go away: a refusal is the guard working, and the pane you delete by hand is the one holding the work.
5. Reply with the target, the handoff id, the document path and the placement or command. No recap of the briefing.

## Notes

- Models: pass only what the user requested; opencode wants `provider/model`, and `herdr-handoff` drops a bare model for opencode with a note.
- The automatic context trigger (`herdr-eng handoff context-hook claude`) exists but is not installed by default; opt in as described in `docs/operations/handoff.md`.
