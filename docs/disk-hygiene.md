# Disk hygiene (default on)

Three pieces, all on by default. Agents delete what they create through one allow-listed command;
two timers clean up what they forgot.

## 1. `herdr-eng cleanup`: the sanctioned removal command

```
herdr-eng cleanup --scope {worktree,target,docker,tmp} --path P [--merged-ok] [--dry-run] [--json]
herdr-eng cleanup --print-run-args        # docker run arguments that make a container reapable
```

It replaces raw `rm -rf`, `git worktree remove` and `docker rm`, which the Claude Code permission
classifier blocks. It is not a bypass:

| Check | Applies to |
|---|---|
| path is inside an agent-owned root and is not the root itself | all path scopes |
| no `..`; no symlink anywhere below the root; inner symlinks are never followed | all path scopes |
| entry is owned by the current uid | all path scopes |
| no running process has its cwd, executable or an open file inside; if that cannot be verified (lsof/proc failure or timeout) the command refuses | worktree, target, tmp |
| refuses repo main checkouts and non-worktrees | worktree |
| refuses a dirty tree (uncommitted or untracked files) | worktree |
| refuses commits that are on no remote ref; `--merged-ok` relaxes only if the commits are patch-merged into `origin/main` or `gh` reports the PR merged/closed for the same head branch, head SHA and origin owner (fork PRs and reused branch names do not match) | worktree |
| must be a Cargo `target` (build markers, sibling `Cargo.toml`), no tracked files, no active build lease (refused while one is held) | target |
| only under temp roots (`$TMPDIR`, `/tmp`, `$TMPDIR/claude-<uid>`); refuses a git repo/worktree or any directory containing a `.git` entry (use `--scope worktree`) | tmp |
| refuses ignored-but-present files that are not rebuildable output (`.env`, local DBs; allowed: `target`, `node_modules`, `.venv`, `__pycache__`, plus `disk_hygiene.rebuildable`) | worktree |
| container must carry label `herdr.ephemeral=1` and be stopped | docker |

Default roots (agent-created locations only): `~/.local/share/worktrees`,
`~/.cache/herdr-engineering`, `$TMPDIR/claude-<uid>`. `~/IdeaProjects` and `~/work` are the user's
own repos and are not roots. There is no `--root` flag and no env var: extra roots come only from
`disk_hygiene.roots` in the user config `~/.config/herdr-engineering/config.yaml`; repo-local
config (`./herdr-engineering.yaml`, `./config/...`) is never read for this. Exit codes: 0 done,
1 refused (`--json` prints `{"refused": code}`), 2 usage.

### Permission allow rule

Added by `herdr-eng workflow install` on the FIRST install of the hooks only (part of `scripts/install.sh`, step 7c),
into `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(herdr-eng cleanup:*)"] } }
```

The rule is exactly `Bash(herdr-eng cleanup:*)`. If you remove it, that is respected: it does not
count as drift and `workflow repair`/re-install never re-adds it (only uninstall + install does).
Hosts that installed the hooks earlier must add the rule by hand. The same install adds one line to the SessionStart "Engineering defaults" reminder telling agents
to use `herdr-eng cleanup` and `docker run $(herdr-eng cleanup --print-run-args)`. For pi, codex and
opencode, add the equivalent allow entry for the command prefix `herdr-eng cleanup`.

## 2. Worktree reaper (every 6 h)

`herdr-eng disk-hygiene run --only worktrees [--dry-run] [--json]`

For each repository (config `disk_hygiene.repos`, otherwise main checkouts found under the roots) it
walks `git worktree list` (repos are discovered from the worktrees found in the agent roots; only
worktrees inside an agent root are ever reaped, never the user's own). It decides by PR state and git cleanliness, never by path
(see `candidates.py`: "never remove a worktree by path heuristics"):

| Situation | Action |
|---|---|
| PR merged or closed, tree clean, nothing unpushed | remove worktree and the `target/` inside it |
| no PR, idle longer than `worktree_ttl_days` (7), clean, fully pushed | remove |
| PR open; PR state unknown (gh missing/offline/not GitHub) | keep |
| activity within `min_idle_hours` (2); locked; running process; dirty; unpushed | keep |
| registration whose directory is gone | `git worktree prune` |

PR state comes from `gh pr list --head <branch> --state all`, matched on head branch, head SHA == the worktree HEAD and head repo owner == origin owner (otherwise no PR: the TTL path); a gh failure keeps the worktree; `pr_source: git` uses local ancestry
into `origin/main` for offline hosts. The removal itself is the `cleanup --scope worktree` code, so
the reaper cannot do what the command refuses. A `CARGO_TARGET_DIR` outside the worktree is not
touched here; use `herdr-eng build-cleanup`.

## 3. Docker sweep (nightly 03:30)

`herdr-eng disk-hygiene run --only docker [--dry-run] [--json]`

1. remove containers labelled `herdr.ephemeral=1` that are exited/created/dead for more than 1 h;
2. `docker volume prune -f --all --filter label=herdr.ephemeral=1`.

`docker builder prune` has no label filter and `docker image prune` removes any dangling image, so
both are opt-in (`docker.builder_prune: true` with `builder_keep_storage`, `docker.image_prune: true`)
and off by default.

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
  roots: []              # extra agent roots (user config file only)
  worktree_ttl_days: 7
  min_idle_hours: 2
  pr_source: gh          # gh | git
  worktrees: {enabled: true}
  docker: {enabled: true, min_age_hours: 1, builder_prune: false, builder_keep_storage: 5GB, image_prune: false}
  rebuildable: []        # extra ignored dir names a worktree may hold (default target, node_modules, .venv, __pycache__)
```

Opt out: `enabled: false` in the user config file (the timers read exactly that file; `HERDR_DISK_HYGIENE=0` only affects interactive runs, a shell export never reaches launchd/systemd), `scripts/install.sh --no-disk-hygiene` /
`HERDR_INSTALL_DISK_HYGIENE=0`, or `scripts/install-disk-hygiene.sh uninstall`. The last JSON report
is in `<state>/disk-hygiene.json`.

## Install and roll out

```
scripts/install-disk-hygiene.sh install            # systemd user timers (Linux) / LaunchAgents (macOS)
scripts/install-disk-hygiene.sh status
herdr-eng disk-hygiene run --dry-run --json        # preview what would be removed
```
