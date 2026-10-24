-- Event envelope v2 follow-up (T02 review): the sequence domain is
-- (actor id, source), not the bare source string, so one principal cannot burn
-- another principal's (source, source_sequence) by sending first.
-- Safe to re-run. Until applied, the 0003 global index still enforces the old
-- (stricter) rule.

ALTER TABLE fabric_event_sources ADD COLUMN IF NOT EXISTS source_actor text NOT NULL DEFAULT '';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
    WHERE i.indrelid = 'fabric_event_sources'::regclass AND i.indisprimary AND a.attname = 'source_actor'
  ) THEN
    ALTER TABLE fabric_event_sources DROP CONSTRAINT IF EXISTS fabric_event_sources_pkey;
    ALTER TABLE fabric_event_sources ADD PRIMARY KEY (source_actor, source);
  END IF;
END $$;

DROP INDEX IF EXISTS uq_fabric_events_source_sequence;
CREATE UNIQUE INDEX IF NOT EXISTS uq_fabric_events_actor_source_sequence
  ON fabric_events ((COALESCE(actor ->> 'id', '')), source, source_sequence)
  WHERE source IS NOT NULL AND source_sequence IS NOT NULL;
