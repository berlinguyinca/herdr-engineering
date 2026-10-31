-- Retention sweep index. Every 5 minutes the web server deletes old token_usage
-- events (DurableEventSink.prune_events_before):
--   DELETE FROM fabric_events WHERE entity_type=$1 AND source_timestamp < $2
-- idx_fabric_events_stream (entity_type, entity_id, sequence) only narrows that to
-- the entity type, so each sweep visited every token_usage row. This index lets it
-- read just the rows past the cutoff.
--
-- The Migrator runs each file in a transaction, so this cannot be CONCURRENTLY and
-- holds a write lock on fabric_events while it builds. On a large table, build it
-- first by hand (IF NOT EXISTS then makes this a no-op):
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fabric_events_type_source_ts
--     ON fabric_events (entity_type, source_timestamp);
CREATE INDEX IF NOT EXISTS idx_fabric_events_type_source_ts
  ON fabric_events (entity_type, source_timestamp);
