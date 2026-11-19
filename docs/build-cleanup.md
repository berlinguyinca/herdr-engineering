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

Running inside a Herdr terminal alone does not acquire this lock. The Pi extension
currently wraps only recognized heavy commands; for other builds, cleanup also
checks same-user process working directories, executables, and file descriptors.
An inaccessible process inside a local Herdr server process tree blocks deletion.
Unreadable daemons outside those managed trees are excluded; this policy assumes
builds run inside Herdr, as on this host. Accessible processes outside Herdr are
still checked. Process checks
are conservative but cannot provide the same race-free exclusion as the wrapper;
route build commands through `build-coordinate` for lock protection. Already
running builds from before the upgrade rely on the process checks until they exit.

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

A sweep takes `build-cleanup.lock` exclusively (one sweep at a time) and `build-maintenance.lock` shared, so it never waits for a running build (the guard runs inside builds) and only yields to the exclusive maintenance jobs. A running build is protected per target: the wrapper holds a shared lock on `targets/<name>.lock` from before cargo starts until the build ends, and the sweep takes it exclusively (non-blocking) right before deleting and keeps it through the delete. A target whose lock is held is skipped (`build in progress`); a build that starts during a delete waits for it and then starts on a fresh target. A subdirectory a sweep cannot read is skipped with a warning; the rest of its root is still swept. A root that is missing or not canonical is skipped with a logged reason; it never aborts the sweep.

Targets still in use (live process, build lease, open file, held build lock) are
never removed in any tier. When the build's filesystem is at or above `refuse_builds_percent`
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

## Reserve and removal locks

When no storage tier keeps `min_free_disk_percent` free, the cargo wrapper runs one cleanup, limited to the roots on
the filesystem that missed the reserve and applying only the configured `pressure_percent` rules, looks at the tiers
again and exits 75 if none fits (the message says "even after cleaning up" only when a cleanup ran; with
`build_cleanup.enabled: false` none does). The guard's own refusal still runs the sweep in pressure mode.
A failed sweep does not start the 10-minute cooldown.

A managed target has one lock file, `<name>.lock`, beside it. Builds and the sweep both create it if it is missing
(the sweep skips that target on a full disk), neither ever deletes it, and the build holds a shared lock for as
long as its wrapper process lives (the lock is close-on-exec and kept by the wrapper, which waits for cargo, so a
daemon cargo starts does not keep the target locked). The sweep takes it exclusively without waiting and skips the target when held. A dry run takes no lock
file and creates none. Unmanaged targets are checked against a fresh process snapshot right before each removal.
