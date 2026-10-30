-- Artifact registry extensions for the unified workspace (P03 foundation). Additive and idempotent.
-- Design: docs/specs/herdr-unified-workspace-20261028/design/ARTIFACT_INTEGRATION.md
--
-- DOWN (drops only objects created here; the registry tables themselves are untouched):
--   DROP TRIGGER IF EXISTS artifact_versions_freeze ON artifact_versions;
--   DROP FUNCTION IF EXISTS artifact_versions_freeze();
--   DROP TRIGGER IF EXISTS artifact_lifecycle_events_append_only ON artifact_lifecycle_events;
--   DROP FUNCTION IF EXISTS artifact_lifecycle_events_append_only();
--   DROP TABLE IF EXISTS artifact_upload_parts, artifact_upload_sessions, artifact_publish_ledger,
--     artifact_lifecycle_events;
--   DROP INDEX IF EXISTS idx_artifact_bindings_ctx_dedup, idx_artifact_bindings_source_event,
--     idx_artifact_bindings_task, idx_artifact_bindings_project, idx_artifact_bindings_dedup;
--   CREATE UNIQUE INDEX idx_artifact_bindings_dedup ON artifact_bindings
--     (artifact_id, mission_id, COALESCE(session_id, ''), COALESCE(stage, ''));
--   ALTER TABLE artifact_bindings DROP COLUMN IF EXISTS task_id, DROP COLUMN IF EXISTS project_id,
--     DROP COLUMN IF EXISTS link_kind, DROP COLUMN IF EXISTS version_id, DROP COLUMN IF EXISTS source_event_id,
--     DROP COLUMN IF EXISTS ctx_key;
--   ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_lifecycle_known, DROP COLUMN IF EXISTS archived_at,
--     DROP COLUMN IF EXISTS tombstoned_at, DROP COLUMN IF EXISTS purge_after;
--   ALTER TABLE artifact_blobs DROP COLUMN IF EXISTS orphan_marked_at;
--   ALTER TABLE artifact_versions DROP COLUMN IF EXISTS origin_room_id, ... (the origin_*, model_id, prompt_revision,
--     skill_revision, commit_sha, redacted_inputs and purged_at columns below);

-- Per-version provenance. Plain text, deliberately NO foreign keys: a version must survive deletion of the session,
-- agent, task or room it came from (P03 gate). Values are self-reported by the harness, not attested.
ALTER TABLE artifact_versions
  ADD COLUMN IF NOT EXISTS origin_room_id     text,
  ADD COLUMN IF NOT EXISTS origin_task_id     text,
  ADD COLUMN IF NOT EXISTS origin_issue_ref   text,
  ADD COLUMN IF NOT EXISTS origin_project_id  text,
  ADD COLUMN IF NOT EXISTS origin_session_ref text,
  ADD COLUMN IF NOT EXISTS origin_agent_ref   text,
  ADD COLUMN IF NOT EXISTS model_id           text,
  ADD COLUMN IF NOT EXISTS prompt_revision    text,
  ADD COLUMN IF NOT EXISTS skill_revision     text,
  ADD COLUMN IF NOT EXISTS commit_sha         text,
  ADD COLUMN IF NOT EXISTS redacted_inputs    jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS purged_at          timestamptz;
CREATE INDEX IF NOT EXISTS idx_artifact_versions_origin_task ON artifact_versions(origin_task_id) WHERE origin_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_versions_origin_project ON artifact_versions(origin_project_id) WHERE origin_project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_versions_origin_issue ON artifact_versions(origin_issue_ref) WHERE origin_issue_ref IS NOT NULL;

-- A committed version's content and provenance never change and the row is never deleted. Execution attestation
-- columns (validation, job_id), the lifecycle state and the purge marker may still move.
CREATE OR REPLACE FUNCTION artifact_versions_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'artifact versions are immutable evidence; tombstone the artifact instead' USING ERRCODE = '55000';
  END IF;
  IF (to_jsonb(NEW) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['state', 'committed_at', 'purged_at', 'validation', 'job_id']) THEN
    RAISE EXCEPTION 'artifact version content and provenance are immutable' USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS artifact_versions_freeze ON artifact_versions;
CREATE TRIGGER artifact_versions_freeze BEFORE UPDATE OR DELETE ON artifact_versions
  FOR EACH ROW EXECUTE FUNCTION artifact_versions_freeze();

-- Bindings are for lookup only. task/project join room/issue/commit as nullable context.
ALTER TABLE artifact_bindings
  ADD COLUMN IF NOT EXISTS task_id         text,
  ADD COLUMN IF NOT EXISTS project_id      text,
  ADD COLUMN IF NOT EXISTS link_kind       text NOT NULL DEFAULT 'origin',
  ADD COLUMN IF NOT EXISTS version_id      text,
  ADD COLUMN IF NOT EXISTS source_event_id text,
  ADD COLUMN IF NOT EXISTS ctx_key         text;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifact_bindings_link_kind_known'
                 AND conrelid = 'artifact_bindings'::regclass) THEN
    ALTER TABLE artifact_bindings ADD CONSTRAINT artifact_bindings_link_kind_known
      CHECK (link_kind IN ('origin', 'reference', 'derived'));
  END IF;
END $$;
-- The legacy dedupe index would reject a second binding of one artifact to a different task while mission/session/stage
-- repeat. Keep its exact semantics for legacy rows only; rows written by the lookup API carry ctx_key instead.
DROP INDEX IF EXISTS idx_artifact_bindings_dedup;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_bindings_dedup
  ON artifact_bindings (artifact_id, mission_id, COALESCE(session_id, ''), COALESCE(stage, '')) WHERE ctx_key IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_bindings_ctx_dedup
  ON artifact_bindings (artifact_id, ctx_key) WHERE ctx_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_bindings_source_event
  ON artifact_bindings (artifact_id, source_event_id) WHERE source_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_bindings_task ON artifact_bindings(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_bindings_project ON artifact_bindings(project_id) WHERE project_id IS NOT NULL;

-- Lifecycle: active <-> archived, active/archived -> tombstoned (restorable until bytes are purged).
-- 'retracted' is the visibility withdrawal written by the lineage gate.
ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS archived_at   timestamptz,
  ADD COLUMN IF NOT EXISTS tombstoned_at timestamptz,
  ADD COLUMN IF NOT EXISTS purge_after   timestamptz;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'artifacts_lifecycle_known'
                 AND conrelid = 'artifacts'::regclass) THEN
    ALTER TABLE artifacts ADD CONSTRAINT artifacts_lifecycle_known
      CHECK (lifecycle IN ('active', 'archived', 'retracted', 'tombstoned'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_artifacts_purge_due ON artifacts(purge_after) WHERE lifecycle = 'tombstoned';

CREATE TABLE IF NOT EXISTS artifact_lifecycle_events (
  event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  artifact_id  text NOT NULL REFERENCES artifacts(artifact_id),
  action       text NOT NULL CHECK (action IN ('archive', 'restore', 'tombstone', 'purge')),
  from_state   text NOT NULL,
  to_state     text NOT NULL,
  actor        text NOT NULL,
  reason       text NOT NULL DEFAULT '',
  request_key  text,
  detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_lifecycle_request
  ON artifact_lifecycle_events(artifact_id, request_key) WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_lifecycle_artifact ON artifact_lifecycle_events(artifact_id, event_id);
CREATE OR REPLACE FUNCTION artifact_lifecycle_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'artifact lifecycle events are append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS artifact_lifecycle_events_append_only ON artifact_lifecycle_events;
CREATE TRIGGER artifact_lifecycle_events_append_only BEFORE UPDATE OR DELETE ON artifact_lifecycle_events
  FOR EACH ROW EXECUTE FUNCTION artifact_lifecycle_events_append_only();

-- Two-phase publish bookkeeping: one row per committed version. 'committed' means the graph edges may be missing.
CREATE TABLE IF NOT EXISTS artifact_publish_ledger (
  version_id      text PRIMARY KEY REFERENCES artifact_versions(version_id),
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  source_event_id text NOT NULL,
  state           text NOT NULL DEFAULT 'committed' CHECK (state IN ('committed', 'linked')),
  attempts        int NOT NULL DEFAULT 0,
  last_error      text,
  committed_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  linked_at       timestamptz
);
CREATE INDEX IF NOT EXISTS idx_artifact_publish_ledger_pending
  ON artifact_publish_ledger(committed_at) WHERE state = 'committed';

ALTER TABLE artifact_blobs ADD COLUMN IF NOT EXISTS orphan_marked_at timestamptz;

-- Resumable uploads proxied through the control plane (no browser-to-S3 traffic, no CSP change).
CREATE TABLE IF NOT EXISTS artifact_upload_sessions (
  upload_id        text PRIMARY KEY,
  artifact_id      text NOT NULL REFERENCES artifacts(artifact_id),
  principal_id     text NOT NULL,
  idempotency_key  text,
  filename         text NOT NULL,
  media_type       text NOT NULL,
  size_bytes       bigint NOT NULL CHECK (size_bytes > 0),
  sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  part_size        bigint NOT NULL CHECK (part_size > 0),
  part_count       int NOT NULL CHECK (part_count > 0),
  state            text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'complete', 'aborted', 'expired')),
  storage_key      text,
  created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at       timestamptz NOT NULL,
  completed_at     timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_upload_idem
  ON artifact_upload_sessions(principal_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_upload_open ON artifact_upload_sessions(expires_at) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS artifact_upload_parts (
  upload_id    text NOT NULL REFERENCES artifact_upload_sessions(upload_id),
  part_number  int NOT NULL CHECK (part_number > 0),
  size_bytes   bigint NOT NULL CHECK (size_bytes > 0),
  sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  storage_key  text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (upload_id, part_number)
);
