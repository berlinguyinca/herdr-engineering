-- Member-artifact purge (plan 5, Task 9 fix round 2): third-party report feedback must be anonymisable when the
-- artifact is purged, but artifact_report_feedback / artifact_report_checks are append-only.  The trigger function
-- now lets exactly one UPDATE through: one that only replaces body and actor with '[removed]' on feedback, inside a
-- transaction that set herdr.purge_redaction = 'on' (SET LOCAL).  Everything else, and every DELETE, stays refused.
-- Idempotent.  Safe to re-run.
--
-- DOWN:
--   re-create artifact_report_events_append_only() as in 0021_artifact_reports.sql.

CREATE OR REPLACE FUNCTION artifact_report_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'artifact_report_feedback'
     AND current_setting('herdr.purge_redaction', true) = 'on'
     AND NEW.body = '[removed]' AND NEW.actor = '[removed]'
     AND (NEW.feedback_id, NEW.feedback_sequence, NEW.artifact_id, NEW.version_id, NEW.target_kind, NEW.target_id,
          NEW.created_at) IS NOT DISTINCT FROM
         (OLD.feedback_id, OLD.feedback_sequence, OLD.artifact_id, OLD.version_id, OLD.target_kind, OLD.target_id,
          OLD.created_at) THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'report check and feedback events are append-only' USING ERRCODE = '55000';
END;
$$;
