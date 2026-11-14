# Active-session scratch retention

## Intent and observed failure

The maintenance goal requires cleanup of disposable temporary/build output even while its owning coding session remains active, with live work preserved and disk-pressure behavior verified. The current local-only `agent-scratch-reaper` pins a whole UUID session, recognizes only a few name globs, and moves files to a same-disk seven-day quarantine. Current evidence:26GiB in scratch,9.8GiB in quarantine; numbered `dd1.*` and nested `v13/dd` trees carry real DerivedData metadata. Moving those trees did not free space.

## Approach and constraints

Implement a versioned Python-stdlib reaper in herdr-engineering, invoked from an isolated installed runtime. Retaining the name-only/session-wide policy cannot solve the observed growth. Killing sessions or imposing filesystem quotas would disrupt live work. The chosen design combines structural recognition, bounded retention and conservative process/ownership guards; it does not promise to bound currently open live working data.

The managed scratch root defaults to canonical `/tmp/claude-<current uid>` (on macOS `/private/tmp/...`). The existing quarantine is a second, explicitly managed tree. Reject root/home-wide sweeps, foreign ownership/mounts, root aliases, overlapping state/root paths, and symlink traversal. A dry run never deletes or moves application data.

## Candidate classes

- Xcode DerivedData: validate `info.plist` with a string `WorkspacePath` and a generated `Build` tree. Discover regardless of basename. Manage only known generated components (`Build`, compilation/index/module/SDK caches and clean dependency caches); retain `info.plist`, `Logs` and `TestResults` as evidence.
- Cargo: a `target` beside `Cargo.toml` with real Rust metadata/fingerprint markers, untracked by the enclosing repository.
- SwiftPM: validate workspace-state metadata; generated output is disposable, while modified/unpublished nested dependency repositories remain protected.
- General abandoned sessions retain the previous conservative48h/non-live policy and seven-day recovery period. Their quarantine is capped at4GiB: do not admit more general data when it cannot fit; report it rather than silently discarding it.
- Existing quarantined recognized build components participate in cleanup; they no longer need to consume the boot disk for seven days. Unknown quarantine entries remain protected until their existing retention expires and safety is reverified.

An active parent UUID protects general session data, not independently verified disposable child build components. Unknown directories and valuable worktrees are never inferred disposable merely from names such as `pr62`, `v13`, or `wt`.

## Policy

Every15minutes, age out idle generated output after6h. Also enforce a32GiB disposable-output budget and64GiB minimum available space by removing oldest eligible output after a30minute idle grace. Count actual allocated blocks; avoid overlapping candidates. Quarantine recovery remains7days with4GiB admission cap. Protected bytes may exceed budgets; report the blocker and retained-byte totals explicitly, never force-delete them. Policy values are configurable and validated finite/nonnegative, with tests using small real files and explicit small limits.

## Safety and execution

Use the same exclusive `build-maintenance.lock` as coordinated builds plus a reaper lock to serialize sweeps. Re-read active leases before removal; unreadable registries fail closed. Use machine-readable lsof for open/cwd/mmap paths and process argv references, not a session-wide prefix. Inspection failures prevent application. Protect `.keep` markers and nested repositories with dirty state, unpublished commits, stashes, missing remotes or unverifiable Git state; disable Git hooks/fsmonitor during read-only checks. Never follow internal symlinks when scanning/removing; retain foreign devices/owners/special files. Require tracked-file checks where inside a source repository.

Revalidate identity, recognition, freshness and live use immediately before removal. Claim output with same-filesystem rename into private state, verify the claimed inode, repeat protection checks against original and claimed paths, restore on a changed/protected claim, then remove with symlink-safe rmtree. Never copy across volumes to quarantine. Keep registry/maintenance exclusion for the whole transaction. Process checks protect uncoordinated jobs conservatively; coordinated jobs have lock exclusion. Rebuildable outputs may be regenerated after removal; source and retained diagnostics remain intact.

## Operations and evidence

Provide default dry-run CLI, explicit `--apply`, JSON status with reclaimed/removed/protected bytes, reasons, budget/pressure state and errors. Bound stored report size; scheduled stdout/stderr go to `/dev/null`, preventing replacement log growth. Preserve the old launcher for rollback, install the new15minute LaunchAgent using an absolute persistent Python `-I` invocation, and verify it loaded. Existing communication timers/runtimes remain independent.

Tests use real files, subprocesses, lsof, Git and file locks: structural recognition, active-parent cache cleanup, unrelated/source/evidence retention, open files/cwd/argv, dirty/unpushed repositories, symlinks, changed candidates, overlap, pressure/byte budget, quarantine admission/expiry and dry-run/apply CLI. Native compiler-produced output and the actual installed host dry-run/application establish live behavior. CI enforces lint, typing,80% statement/branch/function coverage and wheel build, followed by independent exact-head Opus review and normal green merge. Install only the merged release, save before/after reports and remove owned test artifacts.
