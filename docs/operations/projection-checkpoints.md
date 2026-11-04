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

The background worker refreshes on its own connection and thread, one build at a
time. A database advisory lock excludes overlapping materializers. Web startup
reads checkpoints and never creates the recovery schema or index. A missing,
incompatible or damaged capsule falls back to retained history; previously
shipped recovery optimizations remain available until a compatible capsule is
prepared. Code and policy fingerprints bind capsules to their reducer code,
inspector limits/redaction settings and host-alias policy.

Ordinary append-only inspector updates advance the private owner capsule only
when their replay order follows its captured boundary. Service, session and host
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

Only service, session and host histories are compacted in process memory.
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
