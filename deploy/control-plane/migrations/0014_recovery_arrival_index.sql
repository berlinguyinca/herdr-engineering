-- Recovery folds each entity in arrival order. The original stream index orders
-- by sequence only and requires expensive sorting on a large durable journal.
--
-- On an existing large installation, build this before deploying the worker:
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fabric_events_recovery_arrival
--   ON fabric_events(entity_type, entity_id, ingest_timestamp, sequence, event_id);
-- The transactional migrator then observes the existing index and skips the DDL.
-- New installations can build it transactionally on their empty event table.
CREATE INDEX IF NOT EXISTS idx_fabric_events_recovery_arrival
    ON fabric_events(entity_type, entity_id, ingest_timestamp, sequence, event_id);
