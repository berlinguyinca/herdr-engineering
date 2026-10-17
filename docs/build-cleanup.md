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
