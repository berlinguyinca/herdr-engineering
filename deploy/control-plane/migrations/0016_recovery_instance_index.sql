-- On a large running deployment apply migration0015's metadata-only schema first,
-- then build this index CONCURRENTLY outside the transactional migrator. Confirm
-- indisvalid/indisready before applying this idempotent migration receipt.
-- Include the insertion instance so coverage checks don't read every old payload.
CREATE INDEX IF NOT EXISTS idx_fabric_events_recovery_instance
    ON fabric_events ((COALESCE(actor ->> 'id', '')), event_id)
    INCLUDE (actor, recovery_instance);
