# Claude Code workflow hooks

`herdr-eng workflow` registers two managed hooks in `~/.claude/settings.json` so the
default engineering workflow is present in every Claude Code session.

```sh
herdr-eng workflow plan | install [--dry-run] | status | doctor | repair | uninstall [--dry-run]
scripts/install.sh                      # step 7c installs them; --no-workflow-hooks opts out
```

| Event | What it prints | What it never does |
|---|---|---|
| `SessionStart` | `additionalContext`: a <=15 line reminder (delegate multi-file work to sonnet subagents, verify with command output, branch -> PR -> review -> CI green, real deps, never push herdr-engineering, nothing under /tmp, Woodpecker CI, `/handoff`) | run commands, read files, call the network |
| `PreCompact` | `systemMessage` recommending `/handoff` before compaction | run `herdr-handoff` or open a pane |

- Entries carry `herdr-workflow:managed` in the command, which is guarded with
  `[ -x <python> ] && <python> -m herdr_engineering.cli workflow hook <event> || true` and always exits 0. The command uses the interpreter that ran the installer (`sys.executable`), not `herdr-eng` from `PATH`, so an older install on `PATH` cannot make the hooks silently no-op. They are separate from the
  herdr-memory hooks (`herdr-memory:managed`); neither installer touches the other's or any foreign entries.
- Install is idempotent (second run changes nothing), atomic, backed up to
  `~/.local/share/herdr-engineering/workflow/backups/` (`settings.json.bak` is the pristine pre-install copy and is never overwritten; later backups get a timestamp suffix), and skipped when Claude Code is absent.
  Invalid JSON is reported and never rewritten. `doctor` exits 1 on drift or absence; `repair` re-applies.
- The reminder text is a constant in `herdr_engineering/workflow_hooks.py`, not a copy of `AGENTS.md`.

## Permission allow rules (documentation only)

The installer does not touch `permissions`. If you want fewer prompts for the workflow's
read-only commands, add rules yourself under `permissions.allow` in `~/.claude/settings.json`,
for example `Bash(git status:*)`, `Bash(git diff:*)`, `Bash(git log:*)`, `Bash(gh pr view:*)`,
`Bash(herdr-eng workflow doctor:*)`. Never allow `git push` for herdr-engineering.
