-- Where the compaction job (control_plane.fabric_compaction) moves the journal rows it removes. Same columns as
-- fabric_events plus archived_at; no indexes or constraints beyond one on archived_at, so rows keep their original
-- identity and an operator can copy any of them back. The job purges rows older than archive_retention_days.
-- Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS fabric_events_archive;

CREATE TABLE IF NOT EXISTS fabric_events_archive (
  LIKE fabric_events INCLUDING DEFAULTS,
  archived_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fabric_events_archive_archived_at ON fabric_events_archive (archived_at);
