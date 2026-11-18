# Disk hygiene (default on)

Three pieces, all on by default. Agents delete what they create through one allow-listed command;
two timers clean up what they forgot.

## 1. `herdr-eng cleanup`: the sanctioned removal command

```
herdr-eng cleanup --scope {worktree,target,docker,tmp} --path P [--root R] [--merged-ok] [--dry-run] [--json]
herdr-eng cleanup --print-run-args        # docker run arguments that make a container reapable
```

It replaces raw `rm -rf`, `git worktree remove` and `docker rm`, which the Claude Code permission
classifier blocks. It is not a bypass:

| Check | Applies to |
|---|---|
| path is inside an agent-owned root and is not the root itself | all path scopes |
| no `..`; no symlink anywhere below the root; inner symlinks are never followed | all path scopes |
| entry is owned by the current uid | all path scopes |
| no running process has its cwd/open files inside | worktree, target, tmp |
| refuses repo main checkouts and non-worktrees | worktree |
| refuses a dirty tree (uncommitted or untracked files) | worktree |
| refuses commits that are on no remote ref; `--merged-ok` relaxes only if the commits are patch-merged into `origin/main` or `gh` reports the PR merged/closed at the same head | worktree |
| must be a Cargo `target` (build markers, sibling `Cargo.toml`), no tracked files, no active build lease | target |
| container must carry label `herdr.ephemeral=1` and be stopped | docker |

Default roots: `~/work`, `~/IdeaProjects`, `~/.cache/herdr-engineering`, `~/.local/share/worktrees`,
`$TMPDIR/claude-<uid>`. Add more with `disk_hygiene.roots` in the config, `HERDR_CLEANUP_ROOTS`
(`:`-separated) or `--root`. Exit codes: 0 done, 1 refused (`--json` prints `{"refused": code}`), 2 usage.

### Permission allow rule

Installed automatically by `herdr-eng workflow install` (part of `scripts/install.sh`, step 7c),
into `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(herdr-eng cleanup:*)"] } }
```

The same install adds one line to the SessionStart "Engineering defaults" reminder telling agents
to use `herdr-eng cleanup` and `docker run $(herdr-eng cleanup --print-run-args)`. For pi, codex and
opencode, add the equivalent allow entry for the command prefix `herdr-eng cleanup`.

## 2. Worktree reaper (every 6 h)

`herdr-eng disk-hygiene run --only worktrees [--dry-run] [--json]`

For each repository (config `disk_hygiene.repos`, otherwise main checkouts found under the roots) it
walks `git worktree list`. It decides by PR state and git cleanliness, never by path
(see `candidates.py`: "never remove a worktree by path heuristics"):

| Situation | Action |
|---|---|
| PR merged or closed, tree clean, nothing unpushed | remove worktree and the `target/` inside it |
| no PR, idle longer than `worktree_ttl_days` (7), clean, fully pushed | remove |
| PR open; PR state unknown (gh missing/offline/not GitHub) | keep |
| activity within `min_idle_hours` (2); locked; running process; dirty; unpushed | keep |
| registration whose directory is gone | `git worktree prune` |

PR state comes from `gh pr list --head <branch> --state all`; `pr_source: git` uses local ancestry
into `origin/main` for offline hosts. The removal itself is the `cleanup --scope worktree` code, so
the reaper cannot do what the command refuses. A `CARGO_TARGET_DIR` outside the worktree is not
touched here; use `herdr-eng build-cleanup`.

## 3. Docker sweep (nightly 03:30)

`herdr-eng disk-hygiene run --only docker [--dry-run] [--json]`

1. remove containers labelled `herdr.ephemeral=1` that are exited/created/dead for more than 1 h;
2. `docker volume prune -f --all --filter label=herdr.ephemeral=1`;
3. `docker builder prune -f --keep-storage 5GB`;
4. `docker image prune -f` (dangling only).

Unlabelled volumes (databases, registries, corpora) are never touched. Start test containers with

```
docker run $(herdr-eng cleanup --print-run-args) ...     # --rm --label herdr.ephemeral=1
```

or `HERDR_DOCKER_RUN_LABELS="--rm --label herdr.ephemeral=1"` in your harness. Testcontainers users:
set the container label `herdr.ephemeral=1` in the module config.

## Config and opt-out

```yaml
disk_hygiene:            # every key optional; default is on
  enabled: true
  repos: []              # default: discover under roots
  roots: []
  worktree_ttl_days: 7
  min_idle_hours: 2
  pr_source: gh          # gh | git
  worktrees: {enabled: true}
  docker: {enabled: true, min_age_hours: 1, builder_keep_storage: 5GB}
```

Opt out: `enabled: false`, `HERDR_DISK_HYGIENE=0`, `scripts/install.sh --no-disk-hygiene` /
`HERDR_INSTALL_DISK_HYGIENE=0`, or `scripts/install-disk-hygiene.sh uninstall`. The last JSON report
is in `<state>/disk-hygiene.json`.

## Install and roll out

```
scripts/install-disk-hygiene.sh install            # systemd user timers (Linux) / LaunchAgents (macOS)
scripts/install-disk-hygiene.sh status
herdr-eng disk-hygiene run --dry-run --json        # preview what would be removed
```
