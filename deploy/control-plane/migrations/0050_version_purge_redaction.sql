-- Member-artifact purge (final-review fix F3): the file names and commit message of a purged member artifact must
-- not survive in artifact_versions (output_manifest[].name, provenance.message, provenance.entry).  The freeze
-- trigger now lets exactly one extra UPDATE through: one that changes ONLY those fields (everything else in both
-- jsonb columns, and every other column, is compared unchanged) on a PURGED version (NEW.purged_at is set) and
-- only to the value '[removed]' (a changed name, message or entry must equal it), inside a transaction that set
-- herdr.purge_redaction = 'on' (SET LOCAL).  Everything else, and every DELETE, stays refused.
-- Idempotent.  Safe to re-run.
--
-- DOWN:
--   re-create artifact_versions_freeze() as in 0031_artifact_registry_extensions.sql (drop the helpers below).

CREATE OR REPLACE FUNCTION artifact_manifest_without_names(m jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(m) = 'array' THEN
    (SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' THEN e - 'name' ELSE e END ORDER BY o), '[]'::jsonb)
       FROM jsonb_array_elements(m) WITH ORDINALITY AS t(e, o))
    ELSE m END
$$;

-- true when every element of NEW's manifest has the name it had in OLD, or '[removed]'
CREATE OR REPLACE FUNCTION artifact_manifest_names_only_blanked(old_m jsonb, new_m jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN jsonb_typeof(new_m) = 'array' AND jsonb_typeof(old_m) = 'array' THEN
    NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(new_m) WITH ORDINALITY AS n(e, o)
        JOIN jsonb_array_elements(old_m) WITH ORDINALITY AS p(e, o) USING (o)
       WHERE jsonb_typeof(n.e) = 'object' AND n.e -> 'name' IS DISTINCT FROM p.e -> 'name'
         AND n.e -> 'name' IS DISTINCT FROM '"[removed]"'::jsonb)
    ELSE new_m IS NOT DISTINCT FROM old_m END
$$;

CREATE OR REPLACE FUNCTION artifact_versions_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'artifact versions are immutable evidence; tombstone the artifact instead' USING ERRCODE = '55000';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id']) THEN
    IF current_setting('herdr.purge_redaction', true) = 'on'
       AND NEW.purged_at IS NOT NULL
       AND artifact_manifest_names_only_blanked(OLD.output_manifest, NEW.output_manifest)
       AND (NEW.provenance -> 'message' IS NOT DISTINCT FROM OLD.provenance -> 'message'
            OR NEW.provenance -> 'message' = '"[removed]"'::jsonb)
       AND (NEW.provenance -> 'entry' IS NOT DISTINCT FROM OLD.provenance -> 'entry'
            OR NEW.provenance -> 'entry' = '"[removed]"'::jsonb)
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
