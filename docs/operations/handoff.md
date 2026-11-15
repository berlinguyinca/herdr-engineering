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
registered the handoff through `herdr-handoff confirm` — `--no-confirm` skips the wait. Before the close
it asks git about the source worktree (`--source-cwd`, defaulting to `--cwd`, because a linked worktree
resolves to its own path and not the upstream checkout):

* `status: source_dirty` — the worktree has uncommitted tracked changes or commits with no upstream, so
  the close is refused. The successor is live and the handoff succeeded; only the close did not. Commit
  or push, then `herdr-handoff confirm --close-source <handoff_id> --pane <source_pane_id>`.
* `status: worktree_unknown` — git could not be consulted (missing, not a repo, timed out). Absence of
  evidence is not evidence of cleanliness, so this refuses too.
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
