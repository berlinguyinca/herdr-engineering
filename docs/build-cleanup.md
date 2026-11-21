# Automatic Rust build-cache cleanup

The Herdr Engineering cleanup job (a systemd user service on Linux, a launchd interval job on macOS; both on by
default) checks every 15 minutes, whether or not a Herdr server is running (set `build_cleanup.require_herdr_server:
true` to sweep only while a server of the same user runs). It cleans verified Cargo output directories under the
project roots (`~`, `~/work`, `~/IdeaProjects` and `~/.cache` by default) when disk use reaches 85%, oldest first,
stopping at 80%, and any target over `max_target_gb` that has been idle for `size_cap_idle_hours` (default 2) at
every check, whatever the disk use. Under pressure, artifacts must be inactive for at least 24 hours (`min_age_hours`).
Source files, worktrees, Cargo downloads, and Rust toolchains are retained.

Add to `~/.config/herdr-engineering/config.yaml`:

```yaml
build_cleanup:
  enabled: true
  roots: [~/IdeaProjects]   # replaces the default roots (~, ~/work, ~/IdeaProjects, ~/.cache)
  interval_seconds: 900
  high_water_percent: 85
  low_water_percent: 80
  min_age_hours: 24
```

Install using `scripts/build-cleanup.sh`. It installs and starts
`herdr-eng-build-cleanup.service` as a systemd user service at low CPU and I/O
priority. No Herdr binary modification or restart is needed. Re-run the installer
after changing the executable location. Restart the companion after policy changes.

Inspection:

```bash
herdr-eng build-cleanup run --dry-run
herdr-eng build-cleanup status
journalctl --user -u herdr-eng-build-cleanup.service
```

`run` performs a pressure-triggered pass immediately, without requiring Herdr to
be running. `--root PATH` can override configured roots for a manual pass.
A dry run inspects and reports candidates without deleting artifacts.

Builds run through `herdr-eng build-coordinate -- COMMAND` hold a shared host
maintenance lock, including when the concurrency limit is bypassed on timeout.
Retention, migration and cache eviction require the exclusive side, so they skip
their pass while any coordinated build runs. The cleanup sweep takes the shared side
and protects a running build through the build's managed target instead (below). All
participants must use the same `HERDR_ENGINEERING_STATE` location.

Running inside a Herdr terminal alone does not acquire this lock. Instead the sweep refuses
to clean an idle target while a process of this user uses it (see "What the job deletes").
Already running builds from before the upgrade are covered the same way.

The companion does not remove worktrees or retained evidence. Cache deletion
means the next build in that worktree recompiles. Disable with:

```bash
systemctl --user disable --now herdr-eng-build-cleanup.service
```

Other fleet hosts can use the same installer with their own explicit roots and
policy. This feature does not automatically enable deletion on remote hosts.

Fleet updates now install and refresh this companion automatically, preserving
each host's explicit policy. Enable the controller's optional daily update timer
with `scripts/fleet-auto-update.sh`; see [fleet lifecycle](operations/fleet-lifecycle.md).

## Defaults, pressure mode and the disk guard

Cleanup is on by default (roots `~`, `~/work`, `~/IdeaProjects` and `~/.cache`, each swept once even when nested or linked; roots that do
not exist are skipped; the managed `targets/` dirs and the legacy `shared-targets` slot pools of the state dir and of every mounted enrolled storage tier (a `volume_uuid` tier is resolved through its mount) are added automatically and re-evaluated every cycle). Opt out
with `build_cleanup: {enabled: false}`. `scripts/install.sh` and `fleet update` install
the job; `herdr-eng build-hygiene install|status` does it by hand (systemd user unit on
Linux, a launchd interval job on macOS).

```yaml
build_cleanup:
  pressure_percent: 90          # at/above this, targets idle >= pressure_min_age_hours go
  pressure_min_age_hours: 2     # minimum allowed value is 1
  refuse_builds_percent: 90     # cargo wrapper / build-coordinate refuse new builds ...
  refuse_builds_min_free_gib: 50  # ... only when less than this is also available
```

A sweep takes `build-cleanup.lock` exclusively (one sweep at a time) and `build-maintenance.lock` shared, so it never waits for a running build (the guard runs inside builds) and only yields to the exclusive maintenance jobs. A subdirectory a sweep cannot read is skipped with a warning; the rest of its root is still swept. A root that is missing or not canonical is skipped with a logged reason; it never aborts the sweep.

## What the job deletes

The default-on job is narrow. It deletes exactly two kinds of target and nothing else:

1. **Orphaned targets.** A Herdr-managed target (`<root>/targets/<label>-<id>`) whose owning worktree path no longer
   exists: `lstat` of the workspace must raise "not found" and its parent directory must be listable. Any other error
   (permission denied, a missing volume) is not an orphan. A worktree that was moved rather than deleted may still
   have a build running, so an orphan gets the same per-target process scan as an idle target and stays while a process
   uses it or is told to build into it. The decision is made again under the manager lock, then the directory is renamed to `targets/.trash-<name>-<pid>` and deleted after the
   lock is released; a later run removes any `.trash-*` leftover of an interrupted delete. A leftover that cannot be
   deleted is named in `skipped` and the rest of the root keeps sweeping.
2. **Idle targets with a workspace**, past the idle floor (`min_age_hours`, or `pressure_min_age_hours` at or above
   `pressure_percent`), and only while no process of this user is using that target or its workspace W: a process
   whose cwd, executable, open file or (Linux) memory mapping is under the target or W, or a cargo-family process
   (`cargo`, `rustc`, `rustdoc`, `rust-analyzer`, `cargo-*`) whose `CARGO_TARGET_DIR` or `--target-dir` is the
   target. A cargo working elsewhere does not block. The process scan is taken fresh right before each candidate is
   cleaned; a scan that fails closes the pass (`aborted`, with an error), and the refusal message names what blocked
   a target (`blocked` in the run result). The delete is
   `cargo clean --manifest-path <workspace>/Cargo.toml --target-dir <dir>`, so Cargo does its own locking.
   A managed target is cleaned under the manager lock, and its directory, ownership marker and standard
   `CACHEDIR.TAG` are put back in a `finally`, however `cargo clean` ends (even a timeout), so a workspace
   `./target` alias never dangles. `prepare_target` writes the tag for new and existing managed targets, since newer
   cargo refuses to clean a directory without one.
   On Linux the identity comes from the kernel (the `/proc/<pid>/cwd`, `exe` and `fd/*` links and the dev:inode
   columns of `maps`), so a process in another mount namespace (devcontainer, rootless container) is seen even
   though its path does not exist here. The scan runs in a child process with a deadline
   (`HERDR_BUSY_CHECK_SECONDS`, default 120 s) so a dead NFS/sshfs mount cannot hang a sweep or a build's guard;
   on timeout nothing is cleaned, the sweep reports "busy check timed out" and releases its lock. What can be
   seen is limited by the kernel, not by a setting: without root (or `CAP_SYS_PTRACE`) another uid's cwd, exe,
   fd and maps are unreadable even without `hidepid`, so only this user's processes and processes in user
   namespaces this user owns (rootless containers) are detected. Root services (a systemd unit running
   `target/release/svc`), `sudo`-run binaries and rootful containers with a different uid are **not** detected,
   nor are any other user's processes on macOS. Do not run such processes from a managed target.
   This user's own non-dumpable processes (`ping`, file-capability binaries, anything that calls
   `prctl(PR_SET_DUMPABLE, 0)`) have a root-owned `fd/`; they are detected **only through their command line**,
   never by cwd, open files or mappings. Matched: an absolute path in any argument (also the value after any `=`,
   an entry of a `:` list, or a glued `-L/x`) inside the target or the workspace; and, because a relative path
   cannot be resolved without the cwd, a program started by a relative path (`./target/release/server`) or a
   process name (`comm`, which the kernel cuts at 15 characters, so a file whose first 15 characters equal it also
   matches) that equals the name of a regular file anywhere in the target (this errs toward keeping the target;
   a title containing whitespace, such as `sshd-session: user@pts/3`, is not a name and is ignored). Other relative arguments are not looked at. `sudo -i` and setuid-root binaries run with euid 0
   and count as another uid's process.
   Paths are compared by identity, never by spelling: the busy check stats each process path and its ancestors and
   compares (device, inode) with the target and the workspace, so a root typed as `~/work` for `~/Work`, a symlink
   or a bind mount still matches. Ownership is also recorded outside the target, in
   `targets/.owners/<name>.json` (written by `prepare_target`, backfilled for older targets), so a `cargo clean`
   killed by SIGKILL after it removed the marker leaves a directory the manager still owns: the next
   `prepare_target` re-creates the marker. A directory with neither marker nor record is still refused.
   `cargo clean` runs with `--config build.build-dir=<target>` and without `CARGO_BUILD_BUILD_DIR`, so a shared
   `build.build-dir` of other workspaces is never deleted.

Everything else is left alone: targets a lease covers, targets with files tracked by git or anything foreign inside
(links, special files, other owners), and cache-only directories without a workspace to clean through (the old
shared pool). A target whose `cargo clean` fails is kept and named in `skipped`.

Targets a build lease covers, and every target a live process uses, are
never cleaned in any tier (only orphaned targets are, since nothing can build into them). When the build's filesystem is at or above `refuse_builds_percent`
and has less than `refuse_builds_min_free_gib` available, a new cargo/build command
(`build-coordinate` guards rustc, make, cmake, ninja, tsc and `go build|install|generate`, never `go test`/scripts; cargo is guarded by the wrapper, also under `build-coordinate`, for build, check, test, nextest, bench, run and the like, but not `cargo clean`) triggers
one pressure cleanup, run before the `min_free_disk_percent` reserve check so the check sees what it freed (at most every 10 minutes; a sweep that could not start because another one is running does not count, and the guard waits up to two minutes for that sweep and re-measures) and is refused with exit code 75 if the disk is still too full. Setting
`build_cleanup.enabled: false` stops the sweeps (the launchd/systemd job is removed on the
next install/`fleet update`, and `build-cleanup run` exits early) and the guard. `HERDR_DISK_GUARD_OVERRIDE=1` builds anyway.

## Repo-local configuration

A checkout's own `herdr-engineering.yaml` (or `config/herdr-engineering.yaml`) is untrusted input. Only these
keys are read from it: `version`, `web.port`, `tests.adapters`, `ci.provider` and `ci.pileated_extensions`. Everything
else (`build_cleanup`, `fabric_build_cache`, `ci.base_url`, `web.host`, ...) is ignored, and one warning per file names the
ignored keys. A repository being built therefore cannot choose what is deleted, where CI credentials are sent or
which address the web UI listens on. The machine config and an explicit `$HERDR_ENGINEERING_CONFIG` are machine
config and are trusted in full, even when the file sits inside the checkout.

## How full a disk is

One df-style function (`disk_guard.measure`: used / (used + available), as `df` shows it) answers "how full"
everywhere: the `min_free_disk_percent` reserve, the guard's trigger and the sweep's stop target. The guard measures
the tier the build will use (picked the way `select_storage` picks it), not the first tier.

## Reserve cleanup

When no storage tier keeps `min_free_disk_percent` free, the cargo wrapper runs one cleanup, limited to the roots on
the filesystem that missed the reserve and applying only the configured `pressure_percent` rules, looks at the tiers
again and exits 75 if none fits (the message says "even after cleaning up" only when a cleanup ran and removed
something; with `build_cleanup.enabled: false` none does). The guard's own refusal still runs the sweep in pressure
mode. The pressure cleanup deletes exactly the set described above, only with the pressure idle floor, so when every
remaining target is busy it frees nothing and the build is refused. A sweep that failed as a whole (for example the
process list could not be read) starts a 2-minute back-off (not the 10-minute cooldown) and its reason is named in
the refusal; a per-target fault is reported and skipped and the sweep still counts as completed.

`build_cleanup: false` means `enabled: false` with the default thresholds (a bool is accepted everywhere; any other
non-mapping value is a configuration error). `herdr-eng build-cleanup run --root PATH` sweeps the named root even when
`enabled` is false; without `--root` a disabled job reports `disabled`. `build-coordinate -- cargo build` guards
cargo itself whenever the `cargo` on PATH is not the herdr wrapper (measuring the filesystem of its target
directory), so an IDE-style run is refused the same way. The `true`/`false` shorthand is merged into what earlier
config sources set.

## Size cap and home-wide discovery

```yaml
build_cleanup:
  max_target_gb: 50        # 0 turns the cap off
  size_cap_idle_hours: 2   # how long an over-cap target must have been idle; minimum 1
```

**Size cap.** A target larger than `max_target_gb` that has been idle for `size_cap_idle_hours` (default 2, not
below 1) is cleaned by the 15-minute sweep, whatever the disk usage (below `high_water_percent` too). The cap is
enforced by that sweep alone: nothing runs after a build, `build-coordinate` and the cargo wrapper behave exactly as
without a cap, and a target is never cleaned from inside a build. A target is therefore over the cap for up to
`size_cap_idle_hours` plus one sweep interval after its last build; under disk pressure the pressure rules (24 h,
2 h at `pressure_percent`) apply instead. It uses exactly the path of the pressure sweep: a verified Cargo target,
the idle floor, git-untracked, leases, the per-target process scan, the sweep lock, and `cargo clean`. The sweep
result lists it in `reasons` as `size_cap` (`pressure` and `orphan` are the other values). A target that goes over
the cap while a build runs, or whose project is not in git, is kept. Why: a disk reached 100% from stale
`target/debug` copies that no pressure rule had touched.

**Huge targets.** The size check stops walking once the target passes the cap, and the idle time is judged from
Cargo's bookkeeping instead of every file: the target, its profile directories (also under a target triple),
`.fingerprint`, `build` and `incremental` down to the fingerprint files, and every other top-level directory
(`tmp`, `doc`, `package`, ...) to the same depth. So a target of millions of files is judged as cheaply as it is
proven large. The limit: a write that changes no directory mtime (a file rewritten in place, or anything deeper
than the fourth level, for example a scratch file under `build/<crate>/out/a/b/`) is not seen by the idle time;
the process scan and the leases protect a running build. Only under disk pressure is the whole target walked for
its idle time. A target that cannot be decided is reported as a warning (`size cap undecided`), never as a clean pass.

**Content guard.** A target over the cap that is idle enough to be cleaned must first pass the full foreign-content
guard over ALL of its content, not only the part the size walk saw: nothing of another owner or filesystem, no
symlink, no special file, no nested `Cargo.toml` or `.git`. It is a streaming `lstat` walk with no entry limit that
stops at the first violation (the target stays, with a warning). It runs inside the survey deadline; a target too
large to walk in time is not cleaned and gets a warning (`content check ... did not finish in time`). The guard is
repeated right before the process scan. Whatever vanishes during the walk (a build running) is not a violation.

`max_target_gb` must be a number >= 0 and `size_cap_idle_hours` a number >= 1 (a numeric string such as YAML's
`1e-06` is accepted; anything else, `false` and `null` included, falls back to the default, 50 and 2, with one
warning per process).

**Default discovery.** With `roots` unset the sweep searches `~`, `~/work`, `~/IdeaProjects` and `~/.cache` (a root
inside another root is walked once, on its own; a root named twice or through a link is swept once, by its real
path). This widens the earlier narrowed contract of "explicit roots only"; it does so at the user's explicit
request, because the user has dozens of Rust projects and cannot list them all. The walk of every root is bounded
(depth 6, 500,000 directories; past that it keeps what it found and warns), never follows symlinks, and does not
enter `.git`, `node_modules`, `.venv`, `venv`, `.cache` or `__pycache__`; under `~` it also skips dot directories
(`~/.cargo`, `~/.rustup`, `~/.local/share/Trash`, ...) and `Library` (a root you name, such as `~/.cache`, is
searched as named). A directory is a target only when the usual verification accepts it (a `Cargo.toml` beside it
and Cargo's markers). On Linux the walk never stats into a mount point (`/proc/self/mountinfo`); on macOS there is no
such list and the deadline below is the only protection against a dead network mount.

Discovery and size checks of a root run in a separate interpreter (`python -P`, cwd `/`, no inherited file
descriptor, its own process group) with a deadline (`HERDR_SURVEY_SECONDS`, 600 per root): it never holds the sweep's
locks, and one that does not answer is killed with its group (one stuck in the kernel is reaped at the next call). A
root whose discovery did not finish is skipped with a warning and the others still sweep; a survey cut off while it
measured keeps the targets it had finished, with a warning that names how many were examined.

The watcher reuses each root's target list for 6 hours (`build-cleanup-targets.json`, best effort: a disk too full
to write it, or the report, is a warning and the sweep goes on). A malformed cache entry is ignored with a warning;
a cached target that no longer exists is dropped silently. The disk guard before a build uses the cache and never
walks the home directory: with no cache for `~`, the explicit non-home roots are still swept, `~` is reported as
`skipped` ("home directory not searched") and the refusal message says so ("Not searched: ..."). A project whose
nearest git repository is `$HOME` itself, or that is outside any repository, is not cleaned. Setting `roots`
replaces the default.

The busy check (`_inventory`) still walks the whole target once to learn which files a process could hold; for a
very large target this is the slowest step of a pass and is bounded by `HERDR_BUSY_CHECK_SECONDS`.

`scripts/install.sh` (step 7g) installs and enables the cleanup service or timer by default; `--no-build-hygiene` or
`HERDR_INSTALL_BUILD_HYGIENE=0` opts out.
