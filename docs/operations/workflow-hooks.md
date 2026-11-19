# Claude Code workflow hooks

`herdr-eng workflow` registers two managed hooks in `~/.claude/settings.json` so the
default engineering workflow is present in every Claude Code session.

```sh
herdr-eng workflow plan | install [--dry-run] | status | doctor | repair | uninstall [--dry-run]
scripts/install.sh                      # step 7c installs them; --no-workflow-hooks opts out
```

| Event | What it prints | What it never does |
|---|---|---|
| `SessionStart` | `additionalContext`: a <=15 line reminder (delegate multi-file work to sonnet subagents, verify with command output, branch -> PR -> review -> CI green, real deps, push work branches and open PRs autonomously but never push main, nothing under /tmp, Woodpecker CI, do not ask the user about choices these defaults settle) | run commands, read files, call the network |
| `PreToolUse` (matcher `Bash\|Write\|Edit\|MultiEdit\|NotebookEdit`) | a `permissionDecision` of `deny` with a reason, only for the rules below; otherwise nothing | use the network, parse arbitrary shell for `/tmp` writes, or fail a tool call on an internal error (it fails open) |

### PreToolUse guard (`herdr_engineering/workflow_guard.py`)

| Rule | Tool | Decision |
|---|---|---|
| No scratch writes under `/tmp` | `Write`, `Edit`, `MultiEdit`, `NotebookEdit` whose resolved path is under `/tmp` or `/private/tmp` | `deny`, telling the agent to use a gitignored project dir such as `.<project>/<topic>/` |
| No push to main/master | `Bash` containing `git push` (also `git -C <dir> push`, chained with `&&`, `;`, `\|`) whose refspec targets `main`/`master` (`origin main`, `HEAD:main`, `x:refs/heads/main`), or with no refspec while the repo's current branch is `main`/`master` | `deny` |
| Force or destructive herdr-engineering push needs approval | a `git push` whose repo (the `-C` dir, else the hook's `cwd`) has an `origin` URL containing `herdr-engineering` and that forces or deletes: `-f`/`--force`, `--force-with-lease[=...]`, `--force-if-includes`, `--mirror`, `-d`/`--delete`, `--prune`, a `+` refspec or an empty-source `:branch` refspec | `deny` unless the one-shot approval token exists (see below). A plain push of a non-main branch is allowed |
| Unknown repo for a push | a `git push` with no refspec or a `HEAD` refspec after a `cd` the hook cannot resolve | `deny` unless the token exists |
| No self-approval | `Write`/`Edit`/`MultiEdit`/`NotebookEdit` targeting the token path, or `Bash` whose command mentions it | `deny` |

- Scratchpad exception: paths under `/tmp/claude-<digits>/` and `/private/tmp/claude-<digits>/` (the Claude Code harness scratchpad) are allowed.
- `Bash` writes to `/tmp` (redirects, `tee`, `cp`) are deliberately not inspected.
- The branch and `origin` URL come from real `git` calls with a 1 s timeout; there is no network access. Any parse error, timeout, missing repo or malformed stdin prints nothing and exits 0.
- A `deny` from a hook is final for that call. The guard never returns `ask`: Claude Code auto permission mode does not prompt for it, so the rule would do nothing.
- One-shot push approval: the human runs `! touch ~/.local/state/herdr-engineering/allow-push` in the session (the `!` prefix runs it as the user, not the agent). The next guarded push (force/destructive herdr-engineering push, or unknown repo) is allowed and the file is deleted, so each approval covers one tool call (a chained command with several guarded pushes shares it). The token is spent when the push is allowed, so a push that then fails needs a new approval. The `!` prefix runs the command as the user and does not go through PreToolUse hooks. The deny reason tells the agent to ask for exactly this. A push to main/master is denied regardless and does not consume the token. Set `HERDR_WORKFLOW_ALLOW_PUSH` to move the token path (tests do).
- This is a speed bump, not a security boundary: the `Bash` check for the token path is a substring match (`~`, `$HOME` and absolute forms, and `herdr-engineering/allow-push`), so a determined agent can build the path indirectly. It stops accidental and casual self-approval only.
- Opt out: `herdr-eng workflow uninstall` (removes all managed workflow hooks), or install with `--no-workflow-hooks`; to drop only this guard, delete the `PreToolUse` entry whose command contains `herdr-workflow:managed` from `~/.claude/settings.json` (a later `install`/`repair` re-adds it).

- Entries carry `herdr-workflow:managed` in the command, which is guarded with
  `[ -x <python> ] && <python> -m herdr_engineering.cli workflow hook <event> || true` and always exits 0. The command uses the interpreter that ran the installer (`sys.executable`), not `herdr-eng` from `PATH`, so an older install on `PATH` cannot make the hooks silently no-op. They are separate from the
  herdr-memory hooks (`herdr-memory:managed`); neither installer touches the other's or any foreign entries.
- Older versions also registered a `PreCompact` hook that recommended `/handoff`. The automatic handoff controller replaced it, so `install`, `repair` and `uninstall` remove that entry (only a command carrying `herdr-workflow:managed` and `workflow hook PreCompact`; any other `PreCompact` hook, even in the same entry, stays) and `doctor` reports a leftover as drift. A session that still has it keeps working: `workflow hook PreCompact` prints nothing and exits 0. Nothing runs this migration by itself; run `herdr-eng workflow install` once.
- Install is idempotent (second run changes nothing), atomic, backed up to
  `~/.local/share/herdr-engineering/workflow/backups/` (`settings.json.bak` is the pristine pre-install copy and is never overwritten; later backups get a timestamp suffix), and skipped when Claude Code is absent.
  Invalid JSON is reported and never rewritten. `doctor` exits 1 on drift or absence; `repair` re-applies.
- The reminder text is a constant in `herdr_engineering/workflow_hooks.py`, not a copy of `AGENTS.md`.

## Permission allow rules (documentation only)

The installer does not touch `permissions`. If you want fewer prompts for the workflow's
read-only commands, add rules yourself under `permissions.allow` in `~/.claude/settings.json`,
for example `Bash(git status:*)`, `Bash(git diff:*)`, `Bash(git log:*)`, `Bash(gh pr view:*)`,
`Bash(herdr-eng workflow doctor:*)`. Never allow force pushes for herdr-engineering.
