# Automatic Rust build-cache cleanup

The Linux Herdr Engineering companion checks every 15 minutes while a local
Herdr server belonging to the same user is running. It cleans verified Cargo
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
Cleanup requires the exclusive lock, so it skips the pass when any coordinated
build is running and blocks new coordinated builds until deletion ends. All
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
not exist are skipped; the shared cargo target pools, including those on enrolled storage tiers, and legacy per-workspace `targets/` dirs are added automatically and re-evaluated every cycle). Opt out
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

Targets still in use (live process, build lease, open file, held pool-slot lock) are
never removed in any tier. When the build's filesystem is at or above `refuse_builds_percent`
and has less than `refuse_builds_min_free_gib` available, a new cargo/build command
(`build-coordinate` guards rustc, make, cmake, ninja, tsc and `go build|install|generate`, never tests; cargo is guarded by the wrapper, also under `build-coordinate`) triggers
one pressure cleanup (at most every 10 minutes; a sweep that found a build running does not
count) and is refused with exit code 75 if the disk is still too full. Setting
`build_cleanup.enabled: false` stops the sweeps (the launchd/systemd job is removed on the
next install/`fleet update`, and `build-cleanup run` exits early) and the guard. `HERDR_DISK_GUARD_OVERRIDE=1` builds anyway.
