-- Sequence-ordered paging over the archived inspector rows. The inspector snapshot writer's full rebuild
-- (control_plane.inspector_snapshot) reads fabric_events UNION ALL fabric_events_archive, `ORDER BY sequence, event_id`
-- in keyset pages, and looks rows up by (sequence, event_id). fabric_events_archive had no index on sequence (0035 only
-- indexes archived_at, 0052 indexes (entity_id, ingest_timestamp, sequence)), so every 20000-row page scanned and sorted
-- the whole archive. This partial index, with the same predicate as the writer's WHERE, serves the page query, the
-- (sequence, event_id) lookup and the rebuild's duplicate check, and stays limited to the inspector rows.
-- Safe to re-run; skipped when the archive table does not exist.
--
-- The Migrator runs each file in a transaction, so this cannot be CONCURRENTLY and blocks writes to fabric_events_archive
-- while it builds. Only the inspector archive job and fabric_compaction write there (ingest does not touch it), so the cost
-- is paused archiving, not a paused journal. The build sorts the archived inspector rows: expect seconds per million rows, so minutes on an archive
-- of tens of millions. To avoid even that pause, build it first by hand (IF NOT EXISTS then makes this a no-op):
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fabric_events_archive_inspector_sequence
--     ON fabric_events_archive (sequence, event_id)
--     WHERE entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> '';
--
-- DOWN:
--   DROP INDEX IF EXISTS idx_fabric_events_archive_inspector_sequence;

DO $migration$
BEGIN
  IF to_regclass('fabric_events_archive') IS NULL THEN
    RETURN;
  END IF;
  EXECUTE $$CREATE INDEX IF NOT EXISTS idx_fabric_events_archive_inspector_sequence
             ON fabric_events_archive (sequence, event_id)
             WHERE entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> ''$$;
END
$migration$;
