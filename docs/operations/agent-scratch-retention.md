# Agent scratch retention

`agent-scratch-reaper` is a versioned, default-dry-run tool for this user's
`/tmp/claude-UID` scratch tree and its existing recovery quarantine. It identifies
Xcode DerivedData by `info.plist`/`WorkspacePath`, Cargo output by manifest and
compiler markers, and SwiftPM generated output by workspace metadata. Names
such as `dd1.*` and nested locations are supported. It retains `Logs`,
`TestResults`, `info.plist`, unknown folders and source worktrees.

An active session no longer pins all independently verified build output below
it. A `.keep` marker, active process file/cwd/argv reference, live build lease,
tracked output, dirty/unpublished/stashed nested Git work, edited dependency
state, an ambiguous process path or an inspection error still prevents removal.
Read-only Git inspection disables fsmonitor, hooks, pagers, signature commands
and lazy fetching; it ignores inherited Git repository/configuration overrides.
Configured filters, ignored files and hidden index flags prevent dependency
cache removal. Uncoordinated process
checks are conservative; builds using the common maintenance lock get mutual
exclusion. No session is killed or paused. Coordinated builds wait for the shared maintenance
lock while the cleanup transaction holds it. The report records sweep duration.

## Defaults

- Generated output TTL:6hours.
- Pressure:32GiB generated-output budget or less than64GiB available space;
  oldest eligible output may be removed after30minutes idle.
- General session data:48hours idle plus a non-live owner; bounded admission to a
  4GiB recovery quarantine, retained7days. If admission cannot fit, leave it in
  place and report `quarantine_capacity`. Admission reserves the larger of logical
  and allocated file bytes so delayed filesystem allocation cannot over-admit;
  generated-output budgets still use allocated blocks, including sparse files.
- Existing quarantined generated components are also reclaimable. Renaming
  to the same disk is never reported as freed capacity.

Limits never override active or unverifiable work. If protected data prevents a
limit being met, the report says `pressure_blocked` and gives protected bytes and
reasons. These are retention limits over disposable output, not filesystem quotas
on a live build or on unknown user data. Removal means future builds regenerate
that output. Use `.keep` for temporary material that must survive normal retention.

## Inspect and run

```sh
agent-scratch-reaper --dry-run
agent-scratch-reaper --status
agent-scratch-reaper --apply
```

Reports distinguish inspected allocated blocks, removed blocks and the observed
free-space change (which can include concurrent disk activity). They are stored
atomically at `~/.local/state/scratch-reaper/status.json`; detail arrays are
bounded while aggregate counts remain intact. Missing measurements are `null`.
No append-only log is created. `--dry-run` wins when both it and `--apply` appear.

`--root`, `--quarantine`, `--state-dir`, `--gate-state-dir`, TTL/size/free-space
flags and repeated `--session-root` are available for explicit configurations.
The scratch root must be named `claude-<your uid>`; broad home/root sweeps,
symlink root aliases, overlapping data/state roots and cross-volume transaction
state are refused. The gate directory must be the one used by build-coordinate.

A private same-filesystem transaction renames a candidate, verifies its inode
and protections again, and uses symlink-safe removal. A protected claim is
restored without overwriting a recreated source. Interrupted transactions are
restored on the next apply pass; dry runs report pending recovery without moving
application data. Conflicts leave both versions and their private receipt intact
with an error instead of discarding one. Inspect `state-dir/retiring` before any
manual recovery; never delete it blindly.

## Install and rollback

Build the reviewed merged wheel and install it into a persistent internal
virtualenv. Keep the runtime independent of a temporary checkout or optional
external disk. Run it with an absolute interpreter and `-I`:

```sh
/path/to/persistent/venv/bin/python -I -m herdr_engineering.scratch_reaper --dry-run
```

On macOS, render `deploy/agent-scratch-reaper.plist` using plistlib with the actual
absolute runtime/root/quarantine/state paths and the existing job label. It runs
every15minutes with stdout/stderr directed to `/dev/null`. Back up the old launcher
and plist, unload the old job, atomically replace its launcher/configuration, then
bootstrap the rendered job. Preserve one label so two cleanup policies cannot
run concurrently or reappear together after login. Verify `launchctl print`, the
installed dry-run report, an apply report, and the retained source/diagnostic
paths. On Linux use an equivalent oneshot user timer with the same explicit
paths and shared maintenance state; deletion is opt-in per host.

For rollback, unload the new job first, restore the saved launcher/plist and
bootstrap the saved job. Preserve reports and pending transaction receipts.

## Verification

Focused CI uses real files, processes, lsof, Git, locks, compiler output, an
installed wheel and actual CLI invocations on macOS and Linux. It enforces lint,
strict function typing,80% statement/branch/function coverage, and a wheel build.
No mocked process table, filesystem, clock or Git server is used. Live installation
proof records a before/after host report separately from the disposable worktree.
