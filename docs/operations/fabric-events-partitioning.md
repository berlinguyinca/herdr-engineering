# Partitioning `fabric_events` (lazy-loading PR 9)

Off by default and never automatic. Migration 0053 only creates a prepared, empty, partitioned copy
(`fabric_events_part`); traffic moves only when an operator runs the steps below.

## What it does

`fabric_events_part` is LIST-partitioned on `entity_type`: `fabric_events_part_inspector` holds `inspector` rows, the default
partition `fabric_events_part_other` holds everything else (and NULL). Inspector rows are most of the journal and are what boot
replays and autovacuum churn on; they now have their own heap and indexes, so vacuum and index bloat work stays on that
partition, and a later retention job can work on it in isolation.

## Interaction with PR 8 (#751, archive job, migration 0052) and PR 7 (#743)

* Numbering: 0052 is taken by #751 (a partial index on `fabric_events_archive`), so this is **0053**. If #751 merges after
  this, nothing conflicts: they touch different objects. No open PR other than #751 adds a migration.
* `fabric_events_archive` stays an ordinary table; both PRs keep their meaning. #751's `DELETE ... RETURNING -> INSERT` works
  unchanged on the partitioned table (the delete trigger is cloned onto each partition).
* Order: merge and run #751 first if you want the smallest copy (it moves old inspector rows out; target <2M rows).
  If the archive job (or compaction) runs between `backfill` and `cutover`, that is safe: their deletes are logged as `D`
  changes and the cutover catch-up applies them. For the fastest cutover, pause them during the last backfill pass.
* #743 pages inspector history with `(ingest_timestamp, sequence, event_id)` on `entity_type = 'inspector'`; it reads `fabric_events`
  by name and keeps working, now from a single partition (partition pruning on the `entity_type` equality).

## Configure (YAML only, no environment variables)

```yaml
control_plane:
  fabric_partitioning:
    enabled: false          # must be true for backfill, cutover, rollback, drop-old (status always works)
    batch_rows: 5000        # rows per transaction
    pause_seconds: 0.2      # between batches
    lock_timeout_seconds: 5 # cutover/rollback give up instead of queueing writers longer than this
    verify_counts: true     # compare row count, legacy-row count and newest projection_position under the lock
```

Validated in `fleet_config.py`; unknown keys are rejected.

## Runbook

```
herdr-eng control-plane migrate --fleet-config F                      # applies 0053 (cheap: new empty objects only)
herdr-eng control-plane fabric-partition status --fleet-config F
herdr-eng control-plane fabric-partition backfill --fleet-config F    # resumable; --max-batches N to stop early
herdr-eng control-plane fabric-partition backfill --fleet-config F    # run until done=true; repeat to absorb new rows
herdr-eng control-plane fabric-partition cutover --fleet-config F --confirm
```

`cutover` is one transaction: ACCESS EXCLUSIVE on `fabric_events` (bounded by `lock_timeout_seconds`), catch-up
(every row whose `projection_position` is above the backfill's start mark, minus the logged `D` changes), verification, renames,
trigger attach. The lock is held for the catch-up plus two counts, seconds on a 5M-row table; `status` and the result
report `lock_held_seconds`. If anything differs, the transaction rolls back and nothing changed. After cutover, take a fresh
`control-plane checkpoint` (the writer triggers were re-created, positions are preserved).

The old table stays as `fabric_events_unpartitioned` (indexes suffixed `_old`) and costs its disk space until `drop-old`.

## Rollback

* Any time before `drop-old`: `fabric-partition rollback --confirm`. It carries every insert, update and delete made since
  cutover back into the old table (its triggers disabled for the copy so nothing is re-logged), then renames everything back.
  Ingest pauses for the lock exactly as in cutover. The prepared table remains for another attempt.
* Before cutover nothing needs rolling back; to discard the prepared copy: `DROP TABLE fabric_events_part;`
  `DROP TABLE fabric_events_partition_state;` and the three `fabric_events_part_*` functions (see the migration's DOWN note).
* `drop-old` is irreversible; run it only after a soak period.

## Behaviour changes to know

* Unique indexes must contain the partition key, so uniqueness is `(actor, event_id, entity_type)` and
  `(actor, source, source_sequence, entity_type)`, `NULLS NOT DISTINCT`. Retries still dedup (an event keeps its entity type); the one
  rule that weakens is a v2 sequence collision between an inspector row and a non-inspector row of the same actor and source.
  Cutover refuses when any inspector row has a v2 envelope unless `--allow-inspector-envelopes` is passed.
* PostgreSQL 15 or newer (migration 0053 does nothing on older servers; `backfill` then reports that the prepared table is missing).
* The projection writer fence is keyed on `schema.fabric_events`; row triggers on a partition see the partition's name, so the
  partitioned table uses copies of the trigger functions (`fabric_events_part_*`) that pin the key. A test holds a write open
  on a partition and checks the exclusive fence is refused.
* `control-plane checkpoint` skips its CONCURRENTLY position index when the journal is partitioned (migration 0053 already built it).
* A migration that adds a column to `fabric_events` after 0053 makes backfill/cutover/rollback refuse (column lists differ): drop and
  re-prepare the copy (or add the column to both tables) first.
