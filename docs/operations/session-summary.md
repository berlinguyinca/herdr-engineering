# Session summary (`/summary`)

One canonical, read-only skill (`skills/summary/SKILL.md`) that writes an evidence-based
recovery report for the **current** agent session: objective, timeline, verified vs
reported work, test results, repository state, next actions, and an explicit
*Evidence coverage* verdict (`complete` / `partial` / `limited`).

There is no service and no new store. Session history is read from each agent's own
records through `herdr-eng summary history`, which reuses Herdr pane metadata
(`herdr pane get`) and the existing journal projection.

## Install and upgrade

`scripts/install.sh` installs it by default (step 3), and the fleet converges it on every
host (`ansible/tasks/summary-skill.yml`, included from `site.yml` and `fleet-sync.yml`).
By hand:

```bash
herdr-eng summary install                    # global (default)
herdr-eng summary install --scope project    # current repository only
herdr-eng summary install --dry-run          # show what would change
```

Rerun it to upgrade. Global layout:

| What | Where |
| --- | --- |
| Canonical skill (one copy) | `~/.local/share/herdr-engineering/skills/summary/` |
| Claude Code, Codex, Pi, OpenCode skill dirs | symlinks to the canonical copy in `~/.claude/skills`, `~/.codex/skills`, `~/.pi/agent/skills`, `~/.config/opencode/skills` |
| Pi `/summary` | `~/.pi/agent/prompts/summary.md` |
| OpenCode `/summary` | `~/.config/opencode/command[s]/summary.md` (reuses an existing `command/` dir) |
| Ownership record | `…/skills/.summary-manifest.json` (sha256 of every file written) |

Project scope uses `.agents/skills/summary` (read by Codex, Pi, OpenCode), a relative
`.claude/skills/summary` link, `.pi/prompts/summary.md` and `.opencode/commands/summary.md`.

### Never overwrites your files

A file is rewritten on upgrade only while it still matches what the installer last wrote.
A customized prompt, a pre-existing `summary` skill, or a different symlink is reported
as `preserved`, left byte-identical, and warned about on stderr. `herdr-eng summary
uninstall` removes only unmodified files and links that point at our copy.

## Invoke

| Agent | Invocation |
| --- | --- |
| Pi | `/summary` (run `/reload` after installing) |
| Claude Code | `/summary` |
| OpenCode | `/summary` |
| Codex | `$summary`, or `/skills` and pick `summary` |

Codex gets no command file: a bare custom `/summary` is not a documented Codex feature,
and the installer will not shadow a built-in. If a future Codex adds custom slash
commands, add an adapter and a test then.

## Verify the installation

```bash
herdr-eng summary verify          # exit 0 only if skill, all four links and both commands are present
herdr-eng summary verify --json
```

`ok` = matches the canonical source; `stale` = older managed copy (rerun install);
`preserved` = your customized file; `missing` = not installed. Then, in each agent, run
`/summary` (Codex: `$summary`) in a throwaway session; it must open with
`# Session summary` and end with *Evidence coverage*.

Tests: `pytest tests/test_session_summary.py tests/test_session_history.py`.

## What it reads, and its limits

`herdr-eng summary history` picks the session by (1) explicit `--session`, (2) Herdr
pane metadata (`HERDR_PANE_ID` → `herdr pane get`), (3) only then the newest record in
the working directory, labelled `newest-in-cwd (heuristic)` and capped at `limited`.
It reads Claude Code and Pi/Codex JSONL and OpenCode's SQLite database (opened
`mode=ro`), redacts through `control_plane.redact`, and never writes anything.

- **Compaction:** Claude Code, Pi and OpenCode keep pre-compaction turns in their
  records, so they are included and marked. If a record holds only the compaction
  summary, coverage is `partial`.
- **Child agents:** Claude Code (`isSidechain`, `subagents/`) and OpenCode (child
  sessions) expose child records. Pi and Codex expose dispatches and final messages
  only; the report says so.
- Long sessions are elided in the middle (first 60 + last events); the omitted count is
  reported.
- Codex failure detection from tool output is a text heuristic; treat `ok` there as
  weaker evidence than for the other three.
- A report is a snapshot: repository state is read now, not as of the session start.
- `git` is run with `--no-optional-locks`, so even `git status` does not touch the index.
- **Codex:** Herdr reports no conversation for idle Codex panes, so the helper identifies one from Codex's own process log (pid + thread id, newer than the process, primary conversations only; a tie is reported, not guessed). The method shows as `herdr-pane (codex process log)`.
- Herdr's own `journal summary` projection is included only if a journal file already
  exists.
