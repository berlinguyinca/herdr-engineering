-- Owner delete hardening: a durable legal-hold flag, a queue of object keys whose purge is committed but whose bytes
-- are not yet deleted (the record is committed first, bytes after, so a failure never leaves bytes gone with a 'ready'
-- version), and a 'refused' audit action. Additive and idempotent. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_pending_deletes;
--   ALTER TABLE artifacts DROP COLUMN IF EXISTS legal_hold;
--   (the widened artifact_lifecycle_events action check is left in place: 'refused' rows may exist)

ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS legal_hold boolean NOT NULL DEFAULT false;
-- A tombstone with no purge date was a hold before this column existed.
UPDATE artifacts SET legal_hold = true WHERE lifecycle = 'tombstoned' AND purge_after IS NULL AND NOT legal_hold;

CREATE TABLE IF NOT EXISTS artifact_pending_deletes (
  storage_key text PRIMARY KEY,
  queued_at   timestamptz NOT NULL DEFAULT now(),
  attempts    int NOT NULL DEFAULT 0
);

ALTER TABLE artifact_lifecycle_events DROP CONSTRAINT IF EXISTS artifact_lifecycle_events_action_check;
ALTER TABLE artifact_lifecycle_events ADD CONSTRAINT artifact_lifecycle_events_action_check
  CHECK (action IN ('archive', 'restore', 'tombstone', 'purge', 'refused'));
