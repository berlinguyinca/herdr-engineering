-- One row holding the inspector projection as a replay produced it, plus the fabric sequence it covers
-- (watermark) and how many inspector events it consumed. A boot loads it and replays only newer events; any
-- mismatch makes the boot replay everything, so the row is an optimisation and never a source of truth.
-- Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS inspector_snapshots;

CREATE TABLE IF NOT EXISTS inspector_snapshots (
  id             smallint PRIMARY KEY CHECK (id = 1),
  schema_version integer NOT NULL,
  fingerprint    text NOT NULL,
  watermark      bigint NOT NULL,
  event_count    bigint NOT NULL,
  body           bytea NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
