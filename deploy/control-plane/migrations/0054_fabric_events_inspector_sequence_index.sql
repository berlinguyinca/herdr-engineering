-- Sequence-ordered paging over the inspector rows. The inspector snapshot writer's full rebuild
-- (control_plane.inspector_snapshot) pages `WHERE entity_type = 'inspector' AND sequence > $1 ORDER BY sequence LIMIT n`.
-- idx_fabric_events_stream is (entity_type, entity_id, sequence), which cannot serve that ORDER BY, so every page was a
-- parallel seq scan plus a sort of millions of rows (~50 s per page, hours per rebuild on ~10M inspector rows). This
-- partial index serves it as an index scan, and stays limited to the inspector rows.
--
-- The Migrator runs each file in a transaction, so this cannot be CONCURRENTLY and holds a write lock on fabric_events while
-- it builds. On a large table, build it first by hand (IF NOT EXISTS then makes this a no-op):
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fabric_events_inspector_sequence
--     ON fabric_events (sequence) WHERE entity_type = 'inspector';
--
-- DOWN:
--   DROP INDEX IF EXISTS idx_fabric_events_inspector_sequence;

CREATE INDEX IF NOT EXISTS idx_fabric_events_inspector_sequence
  ON fabric_events (sequence)
  WHERE entity_type = 'inspector';
