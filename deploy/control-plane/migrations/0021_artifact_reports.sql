-- Generic report metadata stays pinned to an immutable artifact version.
-- Checks and reader feedback are separate append-only records; a rendered file
-- never implicitly establishes evidence checking or review approval. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_report_feedback, artifact_report_checks;
--   DROP FUNCTION IF EXISTS artifact_report_events_append_only();
--   ALTER TABLE artifact_versions DROP CONSTRAINT IF EXISTS artifact_report_descriptor_object,
--     DROP COLUMN IF EXISTS report_descriptor;
-- (Drops only objects created here; the artifact registry tables are otherwise untouched.)

ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS report_descriptor jsonb;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifact_report_descriptor_object'
                 AND conrelid = 'artifact_versions'::regclass) THEN
    ALTER TABLE artifact_versions ADD CONSTRAINT artifact_report_descriptor_object
      CHECK (report_descriptor IS NULL OR jsonb_typeof(report_descriptor) = 'object');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS artifact_report_checks (
  check_id        text PRIMARY KEY,
  event_sequence  bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  artifact_id     text NOT NULL,
  version_id      text NOT NULL,
  stage           text NOT NULL CHECK (stage IN ('rendered','evidence_checked','reviewed')),
  status          text NOT NULL CHECK (status IN ('passed','failed')),
  notes           text NOT NULL CHECK (char_length(btrim(notes)) > 0 AND char_length(notes) <= 10000),
  evidence_refs   jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_refs) = 'array'),
  actor           text NOT NULL CHECK (char_length(btrim(actor)) > 0 AND char_length(actor) <= 1000),
  created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (version_id, artifact_id) REFERENCES artifact_versions(version_id, artifact_id),
  CHECK (stage = 'rendered' OR status = 'failed' OR jsonb_array_length(evidence_refs) > 0)
);
CREATE INDEX IF NOT EXISTS idx_artifact_report_checks_latest
  ON artifact_report_checks(artifact_id, version_id, stage, event_sequence DESC);

CREATE TABLE IF NOT EXISTS artifact_report_feedback (
  feedback_id       text PRIMARY KEY,
  feedback_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  artifact_id       text NOT NULL,
  version_id        text NOT NULL,
  target_kind       text NOT NULL CHECK (target_kind IN ('report','finding','figure')),
  target_id         text,
  body              text NOT NULL CHECK (char_length(btrim(body)) > 0 AND char_length(body) <= 10000),
  actor             text NOT NULL CHECK (char_length(btrim(actor)) > 0 AND char_length(actor) <= 1000),
  created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (version_id, artifact_id) REFERENCES artifact_versions(version_id, artifact_id),
  CHECK ((target_kind = 'report' AND target_id IS NULL) OR
         (target_kind IN ('finding','figure') AND target_id IS NOT NULL AND
          char_length(btrim(target_id)) > 0 AND char_length(target_id) <= 256))
);
CREATE INDEX IF NOT EXISTS idx_artifact_report_feedback_version
  ON artifact_report_feedback(artifact_id, version_id, feedback_sequence);
CREATE INDEX IF NOT EXISTS idx_artifact_report_feedback_target
  ON artifact_report_feedback(version_id, target_kind, target_id);

CREATE OR REPLACE FUNCTION artifact_report_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'report check and feedback events are append-only' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS artifact_report_checks_append_only ON artifact_report_checks;
CREATE TRIGGER artifact_report_checks_append_only
  BEFORE UPDATE OR DELETE ON artifact_report_checks
  FOR EACH ROW EXECUTE FUNCTION artifact_report_events_append_only();
DROP TRIGGER IF EXISTS artifact_report_feedback_append_only ON artifact_report_feedback;
CREATE TRIGGER artifact_report_feedback_append_only
  BEFORE UPDATE OR DELETE ON artifact_report_feedback
  FOR EACH ROW EXECUTE FUNCTION artifact_report_events_append_only();
