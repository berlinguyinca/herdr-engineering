-- Index for the inspector rows the archive job (control_plane.inspector_archive) moves into fabric_events_archive.
-- Partial, so it stays small however large the archive is (the compaction job's service rows are not indexed). It serves
-- the inspector snapshot's covered-row count (live + archived) and per-session history reads over the archive, which
-- order by (ingest_timestamp, sequence) exactly as inspector_history does on fabric_events. Safe to re-run.
--
-- DOWN:
--   DROP INDEX IF EXISTS idx_fabric_events_archive_inspector;

CREATE INDEX IF NOT EXISTS idx_fabric_events_archive_inspector
  ON fabric_events_archive (entity_id, ingest_timestamp, sequence)
  WHERE entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> '';
