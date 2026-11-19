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
| no running process has its cwd, executable or an open file inside (`ps` + `lsof`; on Linux without `lsof` the `/proc` fds and cwds are used). If neither source works the command refuses. The check runs last, right before each removal, and the reaper repeats it per worktree | worktree, target, tmp |
| refuses repo main checkouts and non-worktrees | worktree |
| refuses a dirty tree (uncommitted or untracked files) and skip-worktree/assume-unchanged entries (hidden edits) | worktree |
| refuses commits that are on no remote ref; `--merged-ok` relaxes only if the commits are patch-merged into `origin/main` or `gh` reports the PR MERGED for the same head branch, origin owner and a head SHA equal to the HEAD being removed (fork PRs, reused branch names and commits made after the lookup do not match). A CLOSED but unmerged PR does not excuse unpushed commits | worktree |
| must be a Cargo `target` (build markers, sibling `Cargo.toml`), no tracked files, no active build lease (refused while one is held) | target |
| only under fixed temp roots (`/tmp`, `/private/tmp`, on macOS the per-user `/var/folders/...` temp dir, and the scratch root `claude-<uid>`; never `$TMPDIR`); refuses anything that is, is inside or contains a git repository of any layout (normal, bare, separate git dir; detected by content, use `--scope worktree`), a `.keep` marker, and a tree with foreign-owned entries, read-only directories or special files (verified before anything is deleted) | tmp |
| under the Claude scratch root only strictly inside a `<session>` directory (never the root, a `<project>` or the session itself; path comparison is by device/inode, so letter case does not matter on macOS). Your own session may clean its subpaths: it is recognised only when one of this process's ancestors is the Claude process recorded in `~/.claude/sessions/<pid>.json` for that session id (an environment variable proves nothing). Any other session must pass the `scratch_reaper` policy: no recent transcript (live) and idle for 48 h | tmp |
| refuses ignored-but-present files that are not rebuildable output (`.env`, local DBs; allowed: `target`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.tox`, `*.egg-info`, `dist`, `build`, `coverage`, `.next`, `.gradle`, `.cache`, ... plus `disk_hygiene.rebuildable`), and any such directory that contains a nested git repository | worktree |
| container must carry label `herdr.ephemeral=1` and be stopped | docker |

Default roots (agent-created locations only): `~/.local/share/worktrees`,
`~/.cache/herdr-engineering`, `/tmp/claude-<uid>`. For `--scope worktree`, autospec's `/tmp/wt-*`
directories are roots too. `~/IdeaProjects` and `~/work` are the user's
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
`workflow install`/`repair` on a host that installed the hooks earlier offers the rule once too (an "offered" marker records it). The same install adds one line to the SessionStart "Engineering defaults" reminder telling agents
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
| PR merged (or closed with nothing unpushed), tree clean | remove worktree and the `target/` inside it |
| PR closed, not merged, commits on no remote | keep |
| no PR, idle longer than `worktree_ttl_days` (7), clean, fully pushed | remove |
| PR open; PR state unknown (gh missing/offline/not GitHub) | keep |
| activity within `min_idle_hours` (2); locked; running process; dirty; unpushed | keep |
| registration whose directory is gone | reported as kept (`missing_directory`); never pruned (the volume may be unmounted) |

PR state comes from `gh pr list --head <branch> --state all`, matched on head branch, head SHA == the worktree HEAD and head repo owner == origin owner (otherwise no PR: the TTL path); a gh failure keeps the worktree; `pr_source: git` uses local ancestry
into `origin/main` for offline hosts. The removal itself is the `cleanup --scope worktree` code, so
the reaper cannot do what the command refuses. The reaper checks the cheap local facts (dirty, hidden index, ignored files) before any network PR lookup, never runs a repo-wide `git worktree prune` (git drops the registration of the worktree it removes), and uses `--git-dir=<common dir>` so bare and separate-git-dir repositories work. Pass `--ttl-days 0` to treat every idle orphan as expired. A `CARGO_TARGET_DIR` outside the worktree is not
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
  rebuildable: []        # extra ignored dir name patterns a worktree may hold (on top of the built-in list above)
```

The config is parsed strictly: opt-outs must be real booleans (`docker: false` and `docker: {enabled: false}` both work; `"false"` or a malformed value does not). An unreadable file or any invalid value disables the destructive jobs and reports status `config_unreadable` instead of reading it as "enabled"; `pr_source` must be `gh` or `git`.

Opt out: `enabled: false` in the user config file (the timers read exactly that file; `HERDR_DISK_HYGIENE=0` only affects interactive runs, a shell export never reaches launchd/systemd), `scripts/install.sh --no-disk-hygiene` /
`HERDR_INSTALL_DISK_HYGIENE=0`, or `scripts/install-disk-hygiene.sh uninstall`. The last JSON report
is in `<state>/disk-hygiene.json`.

## Install and roll out

```
scripts/install-disk-hygiene.sh install            # systemd user timers (Linux) / LaunchAgents (macOS)
scripts/install-disk-hygiene.sh status
herdr-eng disk-hygiene run --dry-run --json        # preview what would be removed
```

## Strictness and exit codes

- Every git call runs through the hardened `scratch_safety.git` runner: `GIT_*` stripped, no user or system git config, no fsmonitor or hooks, output read as exact bytes (`status -z`, so odd file names are never quoted or altered). A repo or user setting such as `status.showUntrackedFiles=no` cannot hide ignored files from the checks. Two things still come from the user's environment, so the checks agree with the user's own git about what is ignored: `core.excludesFile` (read with `--includes`, so a value set in an included config file counts) and, when that is unset, git's default XDG ignore file (`$XDG_CONFIG_HOME/git/ignore`, normally `~/.config/git/ignore`). A file matched by either counts as ignored, not untracked; list it under `rebuildable` to let a worktree containing it be removed.
- OS junk is built in: `.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini` and Finder's `Icon\r` are disposable whether untracked or ignored, never make a tree dirty, and are deleted just before `git worktree remove`. Only regular files count: a junk-named directory (it may be a nested repository) or symlink is judged like any other entry. Any other untracked file still makes the tree dirty.
- Never list IDE directories (`.idea/`, `.vscode/`) under `rebuildable`: they hold state that cannot be rebuilt, for example IntelliJ shelved changes in `.idea/shelf/`. Name only rebuildable output.
- Ignored files are judged with `--ignored=matching`: a file inside a directory that is not itself ignored (`dist/prod.env` under `*.env`) is never rebuildable because of the directory's name.
- Deletion roots come only from fixed system locations and the user's config, never from environment variables. The agent scratch root is `claude-<uid>` under `$CLAUDE_CODE_TMPDIR` (`/tmp` when unset), resolved once per run, and the variable is honoured only when its fully resolved path (no `..`, no symlinked component) is an absolute, user-owned, real directory strictly inside one of the fixed temp roots; otherwise it is ignored with a warning. `$TMPDIR` is never consulted. The live-session guard covers `claude-<uid>` under every fixed temp root plus the env-derived root.
- `core.excludesFile` is resolved as the user's git does: asked once per run for the global chain, and from inside the repository (so a repository's own value, `[includeIf]` and includes apply) only when the global chain has an `[includeIf]` or the repository's config mentions it. In the `/proc` fallback (no `lsof`), zombies and vanished processes are skipped, while a zombie thread-group leader with live threads is inspected through its threads.
- `disk_hygiene.roots` must be a list of absolute paths (`~` allowed); anything else fails closed, on the CLI as in the reaper.
- `--merged-ok` and the reaper accept only a MERGED PR (or patch-merged commits); the dirty/hidden-index/ignored-name checks run before the PR lookup (a worktree that cannot be removed never costs a network call), and the full re-check (HEAD, dirty, hidden index, and the nested-repository walk of rebuildable directories, which runs only here) runs once after it, right before removal.
- An own session needs the ancestor's `sessions/<pid>.json` to match the running process's start time, so a stale file under a recycled pid is not ours.
- Unknown keys in `disk_hygiene`, `worktrees` or `docker` (for example `enable: false`), a non-list `rebuildable` and a malformed `docker.builder_keep_storage` fail closed: every destructive job is disabled.
- `disk-hygiene run` and `status` exit 1 and report `config_error` when the config is unreadable or invalid.
- Without `lsof` on Linux the process check falls back to the real `/proc` plus `ps`; with neither it refuses (`in_use`).
