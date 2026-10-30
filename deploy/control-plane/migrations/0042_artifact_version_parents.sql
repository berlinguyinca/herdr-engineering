-- Git-style artifact history: a version records an ordered LIST of parents (a merge has two) instead of one, and says
-- whether the owner or a collaborating agent committed it.  artifact_collab_attempts logs every collaborator
-- submission, including the ones that conflicted and produced no version.
-- Additive and idempotent.  Safe to re-run.  parent_version_id stays as the first parent for existing readers.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_collab_attempts;
--   ALTER TABLE artifact_versions DROP COLUMN IF EXISTS collaborator_agent_id, DROP COLUMN IF EXISTS created_via,
--     DROP COLUMN IF EXISTS parent_version_ids;

ALTER TABLE artifact_versions
  ADD COLUMN IF NOT EXISTS parent_version_ids text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS created_via text NOT NULL DEFAULT 'owner' CHECK (created_via IN ('owner', 'collaborator')),
  ADD COLUMN IF NOT EXISTS collaborator_agent_id text;

-- artifact_versions_freeze (0031) rejects any update of version content, so the one-off backfill lifts it inside a
-- single DO statement: it is atomic, so the trigger cannot be left disabled.  Re-running touches no rows.
DO $$
BEGIN
  ALTER TABLE artifact_versions DISABLE TRIGGER artifact_versions_freeze;
  UPDATE artifact_versions
     SET parent_version_ids = ARRAY[parent_version_id]
   WHERE parent_version_id IS NOT NULL AND parent_version_ids = '{}';
  ALTER TABLE artifact_versions ENABLE TRIGGER artifact_versions_freeze;
END $$;

CREATE TABLE IF NOT EXISTS artifact_collab_attempts (
  attempt_id            text PRIMARY KEY,
  artifact_id           text NOT NULL REFERENCES artifacts (artifact_id),
  collaborator_agent_id text NOT NULL,
  base_version_id       text,
  head_version_id       text,
  conflict_names        jsonb NOT NULL DEFAULT '[]',
  message               text,
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artifact_collab_attempts_artifact_idx
  ON artifact_collab_attempts (artifact_id, created_at DESC);
