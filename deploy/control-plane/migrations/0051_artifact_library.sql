-- Artifact library: per-owner folders, folder items (manual / suggested / auto filing), share claims (a member
-- keeps a received share in their library), and artifacts.intake ('pending' = pushed by an agent, awaiting the
-- owner's acceptance; every existing row is 'accepted').  The artifact_share_events.kind CHECK also allows
-- 'claimed' (all earlier kinds, including the 0046 annotation kinds, are kept).
-- Additive.  Idempotent.  Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_library_items, artifact_library_folders, artifact_share_claims;
--   DROP INDEX IF EXISTS ix_artifacts_pending_intake;
--   ALTER TABLE artifacts DROP COLUMN IF EXISTS intake;
--   (the widened artifact_share_events_kind_check is left in place: narrowing it would fail on stored 'claimed' events)

CREATE TABLE IF NOT EXISTS artifact_library_folders (
  folder_id  text PRIMARY KEY CHECK (folder_id ~ '^fld_[0-9a-f]{24}$'),
  owner      text NOT NULL,
  name       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifact_library_folders_owner_name ON artifact_library_folders (owner, lower(name));
CREATE INDEX IF NOT EXISTS ix_artifact_library_folders_owner ON artifact_library_folders (owner);

CREATE TABLE IF NOT EXISTS artifact_library_items (
  folder_id   text NOT NULL REFERENCES artifact_library_folders (folder_id) ON DELETE CASCADE,
  artifact_id text NOT NULL,
  source      text NOT NULL CHECK (source IN ('manual', 'suggested', 'auto')),
  filed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (folder_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS ix_artifact_library_items_artifact ON artifact_library_items (artifact_id);

CREATE TABLE IF NOT EXISTS artifact_share_claims (
  member_id  text NOT NULL,
  share_id   text NOT NULL,
  title      text,
  sharer     text,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (member_id, share_id)
);
CREATE INDEX IF NOT EXISTS ix_artifact_share_claims_member ON artifact_share_claims (member_id, claimed_at DESC);

ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS intake text NOT NULL DEFAULT 'accepted';
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'artifacts_intake_check' AND conrelid = 'artifacts'::regclass) THEN
    ALTER TABLE artifacts ADD CONSTRAINT artifacts_intake_check CHECK (intake IN ('accepted', 'pending'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_artifacts_pending_intake ON artifacts (created_by) WHERE intake = 'pending';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'artifact_share_events_kind_check'
                   AND conrelid = 'artifact_share_events'::regclass
                   AND pg_get_constraintdef(oid) LIKE '%claimed%') THEN
    ALTER TABLE artifact_share_events DROP CONSTRAINT IF EXISTS artifact_share_events_kind_check;
    ALTER TABLE artifact_share_events ADD CONSTRAINT artifact_share_events_kind_check
      CHECK (kind IN ('created', 'approved', 'opened', 'denied', 'revoked', 'expired', 'annotated',
                      'annotation_edited', 'annotation_withdrawn', 'annotation_dismissed', 'annotation_addressed',
                      'claimed'));
  END IF;
END $$;
