# Projection checkpoints and coordinated releases

Use the installed `~/.local/bin/herdr-deploy` entry point for production releases.
It selects a committed source revision and holds deployment ownership through
build, preparation, service replacement, verification and any rollback. Another
owner cannot begin a competing rollout. Legacy routing shims fail closed when the
canonical launcher is unavailable; they retain their original bodies and backups.
Use an explicit commit or branch when calling from outside the source checkout.
An intentional deployment to a divergent revision requires `--allow-divergent`.

The target image prepares its projection checkpoint while the previous web
release still serves. Preparation uses the selected fleet configuration, applies
additive migrations, builds the journal-position index concurrently, and publishes
only a capsule that passes checksum and detached restore validation. Failure
aborts before changing host files or services. The protected attempt directory
contains the prewarm log and deployment receipt; do not publish its raw contents.

For an explicit preparation inside the control-plane network:

```sh
herdr-eng control-plane checkpoint --fleet-config /etc/herdr/fleet/fleet-config.yaml
```

Supply `--migrations PATH` when migrations are mounted elsewhere. The command
returns nonzero when preparation or publication fails. JSON output contains only
sanitized errors, counts and recovery timings. A successful ordinary legacy
replay does not count as a published projection checkpoint.

### Prewarm versus the worker's refresh

The running `herdr-cp-worker` refreshes the checkpoint every ~5 minutes and holds
the materializer advisory lock for most of each cycle (measured ~85%). A prewarm
that tried the lock once therefore usually failed with
`another projection checkpoint materializer is active`. The deploy now handles it
two ways:

- Default: the prewarm waits for the lock. `herdr-eng control-plane checkpoint
  --lock-wait SECONDS` polls the lock (every 250 ms) until it is free or the time
  is up, then fails with the same reason. `deploy.sh --checkpoint-lock-wait SECONDS`
  (default 420, longer than one refresh cycle) passes it through. Nothing is
  stopped; the worker simply finds the lock taken for one cycle, logs a warning and
  tries again 5 minutes later. Without `--lock-wait` (worker, web) behaviour is
  unchanged: fail immediately.
- Opt-in `deploy.sh --pause-worker`: stops `herdr-cp-worker` (`docker stop -t 60`)
  before the prewarm and starts it again right after it, on success, failure or
  interrupt (the exit trap). Its other jobs (artifacts, collab bridge) idle for that
  time. A worker that is not running is left alone. The following service
  recreation replaces the worker container anyway.

When a stored candidate is rejected, the server log line
`projection checkpoint candidate rejected: CheckpointInvalid: <reason>` now carries
the validation reason (at most 200 printable characters; only our own
`CheckpointInvalid` messages, never other exception text, DSNs or row data).

The background worker refreshes on its own connection and thread, one build at a
time. A database advisory lock excludes overlapping materializers. Web startup
reads checkpoints and never creates the recovery schema or index. A missing,
incompatible or damaged capsule falls back to retained history; previously
shipped recovery optimizations remain available until a compatible capsule is
prepared. Code and policy fingerprints bind capsules to their reducer code,
inspector limits/redaction settings and host-alias policy.

A bundled compatibility manifest can name a previously verified producer for a
reviewed operational-only patch. Its policy is itself fingerprinted, and aliases
activate only for the exact declared current build. Policy, format, checksum,
frontier and domain validation still apply. Any Python or compatibility-policy
change disables those aliases until explicitly reviewed. Do not regenerate this
binding for reducer changes: let preparation replay retained history instead.

When complete projection checkpoints are active, set
`control_plane.fabric_compaction.rebuild_checkpoint: false` in the central fleet
configuration. This stops rebuilding the redundant legacy row-selection cache;
ordinary journal archival and the complete projection refresh continue. Keep
the last valid legacy cache for fallback. Rebuilding/pruning the large legacy
coverage tables can saturate database storage and queue synchronous ingestion
writes; the rollout investigation observed approximately 19 GB across those
legacy tables. Apply the setting under deployment ownership and restart only
the worker on its existing image after making a validated private config backup.

Fabric compaction deletes interior `service` observation rows (archiving them
first). Those deletes do not invalidate a checkpoint: the loader rebuilds each
affected service stream from its surviving rows, which gives the same projection
as a complete replay because compaction keeps every row that changes a service's
latest state. The change log records no event type, so every `service` delete is
treated this way; deletes of other entity types (outside the retention types),
updates and truncates still require a complete replay. The rebuilt streams count
against the incremental stream budget; if many compacted services exceed it, the
rejection reason names the budget and recovery falls back to a complete replay.

Ordinary append-only inspector updates advance the private owner capsule only
when their replay order follows its captured boundary. Service and session
updates fold into the saved event projection only when every new server-ingest
clock follows that stream's captured maximum. Otherwise recovery reconstructs
the authoritative affected stream or falls back to full replay. These local
ordering checks are not the global watermark: the commit-fenced database
position remains the authority for which mutations belong after a checkpoint.
Operator host-policy revisions and inspector preference history have explicit
ownership, so compact history cannot discard their effects.

`GET /api/v1/health` reports `recovery.mode`, `frontier`, `tail_events`,
`event_rows_scanned` and `elapsed_ms` when the projection path is used. A fallback
includes a reason. Check the protected prewarm log and server log for the failed
phase before retrying. Writer-fence waits are bounded; failure releases the
connection and its session locks. Corrupt checkpoint rows can be ignored safely;
never delete raw journal rows to make recovery pass.

Only service and session histories are compacted in process memory. Complete
host history remains in memory because its operator-policy ordering matters and
its measured payload is small (about 1.9 MB in the release dataset).
Complete retained history remains in PostgreSQL and backs the history APIs and
explicit full exports. Checkpoint publication prunes checkpoint/change metadata,
not raw events. Existing archive/retention jobs retain their separate policies.
Private inspector, host-metric, identity, deployment and collaboration data stays
outside the public activity feed and raw public history endpoints. Mission
activity that the existing ingestion path treats as transient remains transient.

The service catalog opts in with `view=catalog`; the legacy full-list endpoint
keeps its response shape. Catalog cursors bind a bounded snapshot to its query.
Expired snapshots return a restartable 409. Counts and facets cover the whole
scope independently of the loaded page. The UI keeps its last successful rows
while refreshing and shows progress without inventing a completion percentage.
