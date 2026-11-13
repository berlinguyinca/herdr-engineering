# Inspector archive job

`control_plane.inspector_archive` moves old inspector rows from `fabric_events` into
`fabric_events_archive` so the live journal (and the boot replay) stays small. It is
**disabled by default** and has a dry-run mode that only counts.

## Config (fleet YAML only, unknown keys rejected)

```yaml
control_plane:
  inspector_archive:
    enabled: false            # default; nothing runs
    dry_run: true             # default; counts eligible rows, moves nothing
    interval_seconds: 3600
    min_age_days: 14          # floor 7, by ingest_timestamp
    event_types: [inspector.tool, inspector.message]   # file/test kinds are allowed too
    batch_rows: 500000        # per pass
    chunk_rows: 5000          # rows per transaction
    chunk_pause_seconds: 0.25
    max_pass_seconds: 300
    rebuild_checkpoint: true            # rebuild the recovery checkpoint after a drained pass
    keep_checkpoints: 2
    publish_projection_checkpoint: true # publish a fresh projection checkpoint after a drained pass
    projection_lock_wait_seconds: 60
```

To go live: set `enabled: true`, keep `dry_run: true` for a pass or two and read the counts,
then set `dry_run: false`. Rollout is operator-driven; this change enables nothing.

## What moves, and what never does

A row moves only when all of these hold: inspector entity, an allowed event type, older than
`min_age_days`, a v1 row (no v2 envelope), at or below the inspector snapshot watermark, and
at or below the published projection checkpoint frontier (or not yet positioned).
`usage`, `preferences`, `association`, `model`, `lifecycle` and `capabilities` events feed
state and are rejected by config validation.

Each chunk is one statement (`DELETE ... RETURNING` feeding `INSERT INTO archive`) in one
transaction, so a failure rolls both back. A pass is idempotent: re-running finds nothing
left. An advisory lock keeps two workers from running it at once.

## Interactions

- **Inspector snapshot:** the writer and boot check that stored inspector rows up to the
  watermark equal `event_count`. The count now adds archived rows (`archived_count`), so
  archiving does not force a rebuild.
- **Projection checkpoints:** an inspector delete is logged as a `D` change, and a tail with
  one refuses incremental recovery. After a drained pass the job publishes a new checkpoint
  (and rebuilds the recovery checkpoint) so the next boot still uses a checkpoint.
- **Compaction:** the archive purge no longer deletes inspector rows.
- **PR 7 on-demand history (`inspector_history.fetch_page`):** it reads `fabric_events` only.
  Until it also reads `fabric_events_archive`, history older than `min_age_days` ends at the
  archive boundary. Migration 0052 adds a partial index on the archive in the same paging
  order `(entity_id, ingest_timestamp, sequence)` for that follow-up.
