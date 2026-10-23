-- Event envelope v2 follow-up (T02 round 4): event_id is unique per PRINCIPAL, not
-- globally, so one principal cannot squat another principal's client-chosen event_id
-- (deterministic ids such as Pi's lineage hashes are guessable). Safe to re-run.
-- Legacy rows without an actor share the '' domain, so they stay globally unique.

CREATE UNIQUE INDEX IF NOT EXISTS uq_fabric_events_actor_event_id
  ON fabric_events ((COALESCE(actor ->> 'id', '')), event_id);

ALTER TABLE fabric_events DROP CONSTRAINT IF EXISTS fabric_events_pkey;
