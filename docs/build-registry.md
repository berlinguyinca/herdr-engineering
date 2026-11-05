# Build registry (spec 05)

A host-local sqlite registry that makes recorded builds durable and gives cleanup
a second reason not to delete an active workspace. It is an **additional** guard.
The per-host `flock` model (`gate.N` slot locks plus `build-maintenance.lock`,
`LOCK_SH` for builds and `LOCK_EX` for cleanup) is unchanged and remains the
primary safety mechanism.

## When it is active

Only on the recorded path: `herdr-eng build-coordinate --record` (or a cache plan
that implies it). The default exec path never opens the registry. Cleanup reads it
only if `<state_dir>/build-registry.db` already exists.

## Storage

`<state_dir>/build-registry.db` (state dir = `$HERDR_ENGINEERING_STATE/build-gates`
or `~/.local/share/herdr-engineering/state/build-gates`), mode 0600, WAL,
`busy_timeout` 5 s, parameterized statements, `schema_version` table. Bounded on
open: terminal builds older than 30 days are pruned, and builds are capped at 5000
(oldest terminal first; active builds are never pruned). `scope_epochs` rows are kept
so epochs never go backwards.

Tables: `builds`, `attempts`, `transitions` (append-only), `leases`, `scope_epochs`.
No argv, cwd or environment values are stored; `repo_id` is a digest of the git
toplevel path and `trust_domain` is `uid:<uid>`.

## States

`requested -> classified -> admitted -> running -> publishing -> completed`, or
`failed | cancelled | timed_out` from any non-terminal state after `requested`;
`rejected` is terminal. Illegal transitions raise `IllegalTransition`; terminal states
are immutable; every transition is stored with a reason. `completed` requires a
recorded `exit_code` of 0, so a cache hit alone never completes a build. The
wrapper goes `running -> completed|failed|cancelled` directly (`publishing` is
reserved for later specs).

## Idempotent requests

`request_key = sha256(canonical JSON of repo_id, input_digest, toolchain_digest,
profile, features_digest, cmd_kind, trust_domain)`. `register` returns an existing
active build with the same key (non-terminal, last heartbeat within 10 minutes) as
a new correlated attempt with `joined=True` (single-flight is advisory: the caller
still runs its own child). Never across `trust_domain`. **Dirty-tree rule:** if no
deterministic snapshot digest exists (`input_digest` is None) the build is excluded
from dedup. Today `input_digest` is the HEAD sha for a clean tree and None
otherwise, so most agent builds are not deduplicated. `features_digest` and
`target_triple` are not derived yet.

## Leases and fencing

`acquire_lease(scope, attempt, ttl)` runs in `BEGIN IMMEDIATE`; the epoch comes from
`scope_epochs` and strictly increases per scope. Scope is a realpath without a
trailing slash (the build's working directory) or a named scope. One valid lease per
scope; expired means `heartbeat_ts + ttl_s < now`. A takeover after expiry bumps the
epoch; the old holder's `transition`, `finish`, `heartbeat` and `release` raise
`FencedError`. Mutating an attempt that ever held a lease (recorded in `attempt_leases`), even after a takeover or release, without presenting that lease also raises (no stale-writer bypass by omitting the lease).
The recording wrapper heartbeats every `ttl/3` (default ttl 30 s) from a daemon
thread while the child runs. If another owner already holds the workspace lease the
attempt runs unleased (it is still protected by flock).

The registry records the **child's** pid and start time (`record_pid`, at spawn), not the wrapper's: a SIGKILLed wrapper can leave a live build, and `reconcile()` must follow the build.

`reconcile()` releases an expired lease and fails its attempt with
`owner_vanished` only if the owner pid is provably gone (kill 0 says no such process,
or the `/proc` start time differs, or the process is a zombie). A live pid, or an
unreadable `/proc`, is treated as alive.

## Cancellation

`request_cancel(build_id, reason)` only sets a flag. The owning wrapper sees it on its
next heartbeat and sends SIGTERM to its **own** child; nothing signals another pid.
The attempt ends `cancelled`.

## Cleanup

With the registry present, `build_cleanup.run_once` reads active lease scopes after
taking `LOCK_EX` and again right before removal. A candidate whose project directory
equals, contains or lies inside a leased scope is skipped (`active_lease`), in dry-run
too. If the file exists but cannot be read, every candidate is skipped
(`registry_unreadable`) and the status is `warning`. A candidate must still satisfy
the existing Cargo `target` recognition rules at delete time. Source, worktrees,
toolchains and sccache directories are never candidates.

## Failure semantics

Registry faults (locked, disk full, corrupt, read-only) never fail or block a build:
they are swallowed, the finished event lists `registry_unavailable` in `unavailable`,
and the exit code is the child's. After the first fault `unavailable` is set and the event lists `registry_unavailable`; the heartbeat thread and the final `finish` still try (errors stay swallowed), so later calls may succeed. A 0-byte or corrupt db also pauses cleanup (fail closed) until it is
repaired or deleted.

## Rollback

Delete `build-registry.db*` (and stop passing `--record`). Nothing depends on it:
the flock gate and maintenance lock behave exactly as before. Delete only while no
recorded build is running, otherwise those builds lose lease protection for the
rest of their run (flock protection is unaffected).

## Events

`build_started`/`build_finished` carry `build_id` (the registry build id; shared by
joined attempts), `attempt_id`, `request_key` and `joined` when the registry is
available.

## Caveats (security review, spec 05)

* **Signals and pid reuse.** The wrapper waits for the child with `waitid(WNOWAIT)`
  (child stays a zombie, pid cannot be recycled), stops signal forwarding and the
  heartbeat/cancel thread (`BuildRun.halt`), and only then reaps with `wait4`.
* **SQLite on network filesystems.** WAL needs working shared memory and locks; NFS,
  Lustre, BeeGFS and FUSE mounts do not guarantee them. `Registry` logs a warning when
  the state dir is on such a mount (`/proc/self/mountinfo`, advisory only, it does not
  refuse). Use a host-local state dir; never share one registry between hosts (pid
  checks are host-local).
* **Record-mode kill semantics.** In record mode (and whenever the build cache is
  enabled) the build is a child of the wrapper in the same process group. SIGKILL of
  the wrapper orphans the build: it keeps running and keeps holding its slot and
  `build-maintenance.lock` (inherited fds), so cleanup stays blocked and the lease
  expires while the build is alive; `reconcile` then spares it because the child pid is
  recorded. There is no `PR_SET_PDEATHSIG` (it would need a `preexec_fn` while threads
  run, or an exec shim); this is documented behaviour, not fixed. Exec mode (no
  `--record`, cache disabled) is unaffected: the build replaces the wrapper.
* **Cancel** only SIGTERMs the cargo pid; rustc grandchildren and a SIGTERM-ignoring
  child are not escalated.
* **Fenced finish** (takeover, long suspend) still leaves its attempt non-terminal;
  rows are bounded only by reconcile/prune of terminal builds (open item).
