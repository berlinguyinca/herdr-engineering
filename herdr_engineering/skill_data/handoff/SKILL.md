---
name: handoff
description: Hand the current session off to a fresh agent in a new Herdr pane, defaulting to the same harness. Builds a self-sufficient, evidence-based briefing (goal, done, remaining, decisions, risks, validation, resume point, repo/branch/PRs, next commands, constraints) and launches the target. Use when asked to hand off, continue in a fresh session, or move work to claude, pi, codex or opencode.
---

# Session handoff

Usage: `/handoff [pi|claude|codex|opencode]`. With no argument the target is the **same harness** as this session.

This skill composes the briefing and then reuses the `herdr-handoff` CLI (`create`, then `launch`); it does not fork it. It is the *default-target, history-grounded* form of the explicit `/herdr::toClaude`, `/herdr::toPi`, `/herdr::toCodex` and `/herdr::toOpenCode` commands, which use the same two CLI calls. Pi's built-in `/handoff` is a different, planner-worker feature: this skill does not replace it. For a read-only narrative of the session, use `/summary`.

Remain otherwise read-only: never modify files, commit, push, build, deploy or restart anything. The only writes are the handoff files `herdr-handoff` makes under its own outbox and the new pane it opens.

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

## 4. Create and launch

1. Check the CLI: `herdr-handoff --version` must print `0.2.0` or newer (older versions lack the flags below). If it is missing or older, stop and tell the user to update herdr-handoff; do not fall back to a thinner briefing silently.
2. Create, passing the JSON on stdin through a quoted heredoc:
   `herdr-handoff create --goal "<goal>" --source-harness <source> --target-harness <target> --from-json - <<'HANDOFF_EOF'` ... `HANDOFF_EOF`
   Add `--model <id>` only if the user asked for a model. The command prints the queued envelope; take `handoff_id` from it. A validation error means fix the briefing and retry.
3. Launch: `herdr-handoff launch <handoff_id> --cwd <worktree>`.
   - `status: interactive_started`: a new pane (a split of this one, else a new tab or workspace) is running the target with a prompt that tells it to read the handoff document first.
   - `status: manual_launch_required`: herdr is not usable here. Show the user the printed `command` verbatim, run from `cwd`; `resume_command` prints the full resume prompt.
4. Reply with the target, the handoff id, the document path and the placement or command. No recap of the briefing.

## Notes

- Models: pass only what the user requested; opencode wants `provider/model`, and `herdr-handoff` drops a bare model for opencode with a note.
- Automatic triggers (PreCompact and similar) are intentionally not installed; see `docs/operations/handoff.md`.
