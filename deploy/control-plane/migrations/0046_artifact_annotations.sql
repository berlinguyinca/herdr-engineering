-- Artifact annotations (docs/superpowers/plans/2026-10-30-artifact-annotations.md): comments that lab members anchor
-- to a file of a shared artifact version.  The database-free share container appends JSONL lines; the worker ingests
-- them (ingest_key makes that idempotent).  artifact_annotation_ops is the idempotency ledger for edit/delete lines.
-- The artifact_share_events.kind CHECK (0041, unnamed, so Postgres' default name) is widened with the five annotation
-- kinds.  Additive and idempotent.  Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_annotation_ops, artifact_annotations;
--   (the widened artifact_share_events_kind_check is left in place: narrowing it would fail on stored annotation events)

CREATE TABLE IF NOT EXISTS artifact_annotations (
  annotation_id          text PRIMARY KEY CHECK (annotation_id ~ '^ann_[A-Za-z0-9_-]{22}$'),
  artifact_id            text NOT NULL REFERENCES artifacts (artifact_id),
  version_id             text NOT NULL REFERENCES artifact_versions (version_id),
  share_id               text NOT NULL REFERENCES artifact_shares (share_id),
  file_name              text NOT NULL,
  anchor                 jsonb NOT NULL,
  body                   text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
  author_identity        text NOT NULL,
  author_email           text NOT NULL,
  author_role            text NOT NULL CHECK (author_role IN ('member', 'owner')),
  status                 text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'addressed', 'dismissed', 'withdrawn')),
  addressed_in_version_id text REFERENCES artifact_versions (version_id),
  resolved_by            text,
  resolved_at            timestamptz,
  edited_at              timestamptz,
  withdrawn_at           timestamptz,
  created_at             timestamptz NOT NULL,
  ingested_at            timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  ingest_key             text UNIQUE,
  CHECK ((status = 'addressed') = (addressed_in_version_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_artifact_annotations_artifact
  ON artifact_annotations (artifact_id, status, created_at DESC, annotation_id);
CREATE INDEX IF NOT EXISTS ix_artifact_annotations_author
  ON artifact_annotations (share_id, author_identity, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_artifact_annotations_updated ON artifact_annotations (updated_at);

CREATE INDEX IF NOT EXISTS ix_artifact_annotations_author_day ON artifact_annotations (author_identity, ingested_at);

CREATE TABLE IF NOT EXISTS artifact_annotation_ops (
  ingest_key    text PRIMARY KEY,
  annotation_id text NOT NULL,
  op            text NOT NULL CHECK (op IN ('edit', 'delete')),
  at            timestamptz NOT NULL
);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'artifact_share_events_kind_check'
                   AND conrelid = 'artifact_share_events'::regclass
                   AND pg_get_constraintdef(oid) LIKE '%annotated%') THEN
    ALTER TABLE artifact_share_events DROP CONSTRAINT IF EXISTS artifact_share_events_kind_check;
    ALTER TABLE artifact_share_events ADD CONSTRAINT artifact_share_events_kind_check
      CHECK (kind IN ('created', 'approved', 'opened', 'denied', 'revoked', 'expired', 'annotated',
                      'annotation_edited', 'annotation_withdrawn', 'annotation_dismissed', 'annotation_addressed'));
  END IF;
END $$;
