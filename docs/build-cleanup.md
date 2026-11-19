# Automatic Rust build-cache cleanup

The Linux Herdr Engineering companion checks every 15 minutes, whether or not a
Herdr server is running (set `build_cleanup.require_herdr_server: true` to sweep
only while a server of the same user runs). It cleans verified Cargo
output directories under explicitly configured project roots when disk use
reaches 85%, oldest first, stopping at 80%. Artifacts must be inactive for at
least 24 hours. Source files, worktrees, Cargo downloads, and Rust toolchains
are retained.

Add to `~/.config/herdr-engineering/config.yaml`:

```yaml
build_cleanup:
  enabled: true
  roots: [~/IdeaProjects]
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

Cleanup is on by default (roots `~/IdeaProjects`, `~/work`, `~/.cache`; roots that do
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
