-- Event envelope v2 follow-up (T02 round 4): event_id is unique per PRINCIPAL, not
-- globally, so one principal cannot squat another principal's client-chosen event_id
-- (deterministic ids such as Pi's lineage hashes are guessable). Safe to re-run.
-- Legacy rows without an actor share the '' domain, so they stay globally unique.
--
-- ROLLOUT ORDER (see docs/operations/event-id-per-actor-rollout.md):
--   1. deploy the code that tolerates both schemas (bare ON CONFLICT DO NOTHING) FIRST;
--   2. apply this migration afterwards.
-- The previous release issues `INSERT ... ON CONFLICT (event_id)`, which REQUIRES a unique index
-- on event_id alone; once this migration has dropped fabric_events_pkey that statement fails
-- ("no unique or exclusion constraint matching the ON CONFLICT specification"). Do not apply
-- it while an old control plane is still ingesting.
--
-- DOWN (only if the new code is rolled back below this release; it fails while two principals
-- share an event_id - resolve those rows first, e.g. SELECT event_id FROM fabric_events
-- GROUP BY event_id HAVING count(*) > 1):
--   ALTER TABLE fabric_events ADD CONSTRAINT fabric_events_pkey PRIMARY KEY (event_id);
--   DROP INDEX IF EXISTS uq_fabric_events_actor_event_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fabric_events_actor_event_id
  ON fabric_events ((COALESCE(actor ->> 'id', '')), event_id);

ALTER TABLE fabric_events DROP CONSTRAINT IF EXISTS fabric_events_pkey;
