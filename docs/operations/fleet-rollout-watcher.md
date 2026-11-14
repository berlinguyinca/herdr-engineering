# Fleet rollout watcher: operator guide

The watcher polls the merged heads of the watched repos (autospec, pi-engineering, herdr-engineering, ai-sync, claude-config),
plans a rollout, and (only when told to) applies it serially beast, bender, macbook-pro. Design: `docs/specs/herdr-fleet-rollout-watcher-20261104/spec.md`.
Event vocabulary: `fleet-rollout-events.md`. Everything below is CLI-only in v1.

## Enable

The service is `herdr-eng-fleet-watch.service`, a systemd USER unit on beast only (D11). It is installed by
`ansible/tasks/fleet-watch.yml` and is OFF everywhere by default.

| inventory variable (beast) | effect |
|---|---|
| (neither set) | unit file installed, service stopped and disabled |
| `fleet_watch_enabled: true` | service enabled and running; it polls and PLANS only |
| `fleet_watch_enabled: true` and `fleet_watch_apply: true` | a drop-in `herdr-eng-fleet-watch.service.d/apply.conf` adds `--enable-rollouts`, so it also runs rollouts |

The unit runs the installed `~/.local/bin/herdr-eng fleet-watch loop` from `/`, never a checkout. Restart policy: `Restart=on-failure`, 30 s.

Prerequisites on beast: the service has no interactive session, so it relies on stored `gh`/git credentials and on unencrypted ssh keys (there is no `SSH_AUTH_SOCK`).

Exit statuses 2 (refused) and 3 (busy) are not restarted by systemd (`RestartPreventExitStatus=2 3`); the unit also `Wants=` the network. A playbook re-run does not restart a healthy watcher unless the unit or drop-in changed.

**The watcher updates itself.** The watcher runs on beast, and a rollout installs herdr-engineering on beast too, so the running process would keep the old code. After a pass in which a rollout succeeded and really changed herdr-engineering on this host (not a pass that only touched other hosts or other repos), the loop finishes that pass and exits with status 75 (`RestartForceExitStatus=75`, also covered by `Restart=on-failure`), and systemd starts the new build 30 s later (`RestartSec`). The rollout is already saved as succeeded; the new process simply continues polling. Exit 75 is expected after a self-update, not a fault.

**A restart can land mid-rollout.** A playbook re-run that changes the unit or drop-in restarts the service, which can interrupt a rollout that is in the middle of applying a host. That is safe: the restart stops the in-flight host-apply (its process group is killed), the rollout stays `running` in the durable state, and the next start resumes it. `host-apply` and `fleet-watch resume` are idempotent (a host already at the target answers no-change), so nothing is applied twice and nothing needs cleaning up. Prefer re-running the playbook while no rollout is in progress (`fleet-watch` logs `pass N: ...` when idle).

## Disable

Set the variables back to false and re-run the playbook (stops, disables, removes the drop-in), or by hand:
`systemctl --user disable --now herdr-eng-fleet-watch.service`.

## Loop behaviour and limits

- Pass cadence: 120 s +-20 s, with the poller's rate-limit stretch and error backoff (D1/D2). `--interval SECONDS` overrides.
- One loop at a time: flock on `~/.local/state/herdr-fleet-watch/loop.lock` (a second loop exits 3). A pass also takes the per-pass `lock`.
- Soft limit (default 3600 s): one pass running that long only logs a warning.
- Hard limit (default 5400 s): the loop exits 70 only after NO progress (no write to state, outbox, logs) for that long; progress resets it. systemd restarts it and the durable rollout resumes. A long healthy rollout is never killed.
- A failed pass logs only the exception class and the loop continues. SIGTERM stops it cleanly (exit 0) and, like the hard-limit exit, kills any in-flight local `host-apply` child process group instead of leaving it orphaned; a remote ssh phase may still finish on its host, which is safe because host-apply is idempotent.

## Status

`journalctl --user -u herdr-eng-fleet-watch`, and the events in the control plane. Lines look like `pass N: <outcome>`.

## Halted rollouts: resume or abandon (state machine 6.1)

A halted rollout waits for the operator. Fix the cause, then:

- `herdr-eng fleet-watch resume [ROLLOUT_ID]` continues from the durable state (emits `rollout.resumed`). Exit 0 succeeded, 1 halted again, 2 refused, 3 busy.
- `herdr-eng fleet-watch abandon [ROLLOUT_ID]` ends it (emits `rollout.abandoned`, reason `operator`). Abandoned is terminal for those heads; a newer head makes a new rollout.
- `fleet-watch run --resume` still works as a deprecated alias.

Without an id, the single halted rollout is chosen; with several, an id is required. Both accept `--json`.

## The lock (`~/.config/herdr-fleet/fleet.lock.yaml`)

The lock moves ONLY after a rollout fully succeeded, and only if the file already exists (the watcher never invents it). Every SHA must be a commit of `main` in the watcher's own cache clone, as of its last fetch (D3: ancestry in the fetched cache, not a live API head check), otherwise the whole write is refused (`lock.refused`, reason `not_published`), as is a downgrade (`downgrade`) or an unreadable or wrongly shaped file (`lock_invalid`). A refusal does not undo the rollout. If the file is absent the watcher emits `lock.refused` (`lock_invalid`) and never creates it. A refused or absent pin is not retried until a later rollout touches that repo; fix the lock by hand or let the next rollout move it. A lock changed on disk between the watcher's read and its write is refused (`lock_invalid`), and a symlinked lock stays a symlink (the target is replaced). Writes are atomic, keep the file mode, edit only the `repos:` pins (comments preserved), and leave a backup `fleet.lock.yaml.bak-YYYYMMDD` (when that name exists, `fleet.lock.yaml.bak-YYYYMMDD-HHMMSSffffff`, with a `-N` suffix if even that exists, so two writes in one second never overwrite a backup). For a symlinked lock the backups sit beside the real file the link points to, not beside the link. LF and CRLF files are both edited in place and keep their line endings. `herdr-eng fleet lock verify` is read-only: when the watcher state directory does not exist it creates nothing (it checks against a throwaway cache); when it exists, it takes the watcher's pass lock and reports `busy` (exit 3) while a pass runs. A crash between success and pin is repaired on the next pass.

`herdr-eng fleet lock verify [--lock PATH] [--json]` checks every pin. Statuses: `ok malformed_sha fetch_failed unknown_commit not_on_origin_main converged_mismatch unwatched`. Exit 0 when all are ok or unwatched, 1 otherwise, 2 when the file is missing or unreadable, 3 (`busy`) while a watcher pass holds its lock.

## Rollback

Per-host rollback of a failed apply is automatic (D8). To go back to older heads by hand, restore the lock from its `.bak-*`, then `herdr-eng fleet-watch abandon` any halted rollout; downgrading pins through the watcher is refused (`downgrade`).
