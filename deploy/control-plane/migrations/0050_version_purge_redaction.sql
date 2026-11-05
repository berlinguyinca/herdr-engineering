-- Member-artifact purge (final-review fix F3): the file names and commit message of a purged member artifact must
-- not survive in artifact_versions (output_manifest[].name, provenance.message, provenance.entry).  The freeze
-- trigger now lets exactly one extra UPDATE through: one that changes ONLY those fields (everything else in both
-- jsonb columns, and every other column, is compared unchanged) inside a transaction that set
-- herdr.purge_redaction = 'on' (SET LOCAL).  Everything else, and every DELETE, stays refused.
-- Idempotent.  Safe to re-run.
--
-- DOWN:
--   re-create artifact_versions_freeze() as in 0031_artifact_registry_extensions.sql (drop the helper below).

CREATE OR REPLACE FUNCTION artifact_manifest_without_names(m jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(m) = 'array' THEN
    (SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' THEN e - 'name' ELSE e END ORDER BY o), '[]'::jsonb)
       FROM jsonb_array_elements(m) WITH ORDINALITY AS t(e, o))
    ELSE m END
$$;

CREATE OR REPLACE FUNCTION artifact_versions_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'artifact versions are immutable evidence; tombstone the artifact instead' USING ERRCODE = '55000';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id']) THEN
    IF current_setting('herdr.purge_redaction', true) = 'on'
       AND (to_jsonb(NEW) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id',
                                  'output_manifest', 'provenance'])
           IS NOT DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id',
                                  'output_manifest', 'provenance'])
       AND artifact_manifest_without_names(NEW.output_manifest) IS NOT DISTINCT FROM
           artifact_manifest_without_names(OLD.output_manifest)
       AND (coalesce(NEW.provenance, '{}'::jsonb) - ARRAY['message', 'entry'])
           IS NOT DISTINCT FROM (coalesce(OLD.provenance, '{}'::jsonb) - ARRAY['message', 'entry']) THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'artifact version content and provenance are immutable' USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$$;
