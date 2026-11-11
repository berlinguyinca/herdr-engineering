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

## Disable

Set the variables back to false and re-run the playbook (stops, disables, removes the drop-in), or by hand:
`systemctl --user disable --now herdr-eng-fleet-watch.service`.

## Loop behaviour and limits

- Pass cadence: 120 s +-20 s, with the poller's rate-limit stretch and error backoff (D1/D2). `--interval SECONDS` overrides.
- One loop at a time: flock on `~/.local/state/herdr-fleet-watch/loop.lock` (a second loop exits 3). A pass also takes the per-pass `lock`.
- Soft limit (default 3600 s): one pass running that long only logs a warning.
- Hard limit (default 5400 s): the loop exits 70 only after NO progress (no write to state, outbox, logs) for that long; progress resets it. systemd restarts it and the durable rollout resumes. A long healthy rollout is never killed.
- A failed pass logs only the exception class and the loop continues. SIGTERM stops it cleanly.

## Status

`journalctl --user -u herdr-eng-fleet-watch`, and the events in the control plane. Lines look like `pass N: <outcome>`.

## Halted rollouts: resume or abandon (state machine 6.1)

A halted rollout waits for the operator. Fix the cause, then:

- `herdr-eng fleet-watch resume [ROLLOUT_ID]` continues from the durable state (emits `rollout.resumed`). Exit 0 succeeded, 1 halted again, 2 refused, 3 busy.
- `herdr-eng fleet-watch abandon [ROLLOUT_ID]` ends it (emits `rollout.abandoned`, reason `operator`). Abandoned is terminal for those heads; a newer head makes a new rollout.
- `fleet-watch run --resume` still works as a deprecated alias.

Without an id, the single halted rollout is chosen; with several, an id is required. Both accept `--json`.

## The lock (`~/.config/herdr-fleet/fleet.lock.yaml`)

The lock moves ONLY after a rollout fully succeeded, and only if the file already exists (the watcher never invents it). Every SHA must be on GitHub main history and an ancestor of origin/main in the watcher's cache clone (D3), otherwise the whole write is refused (`lock.refused`, reason `not_published`), as is a downgrade (`downgrade`) or an unreadable or wrongly shaped file (`lock_invalid`). A refusal does not undo the rollout. Writes are atomic, keep the file mode, edit only the `repos:` pins (comments preserved), and leave a backup `fleet.lock.yaml.bak-YYYYMMDD` (with `-HHMMSS` when that name exists). A crash between success and pin is repaired on the next pass.

`herdr-eng fleet lock verify [--lock PATH] [--json]` checks every pin. Statuses: `ok malformed_sha fetch_failed unknown_commit not_on_origin_main converged_mismatch unwatched`. Exit 0 when all are ok or unwatched, 1 otherwise, 2 when the file is missing or unreadable.

## Rollback

Per-host rollback of a failed apply is automatic (D8). To go back to older heads by hand, restore the lock from its `.bak-*`, then `herdr-eng fleet-watch abandon` any halted rollout; downgrading pins through the watcher is refused (`downgrade`).
