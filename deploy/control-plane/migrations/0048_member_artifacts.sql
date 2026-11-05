-- Member-owned artifacts (docs/superpowers/plans/2026-10-30-member-owned-artifacts.md, Task 1): per-member usage
-- accounting with optional limit overrides (NULL = inherit the YAML default, itself null = unlimited) and an append-only
-- audit trail of member actions.  Additive and idempotent.  Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_member_creates, artifact_member_events, artifact_member_usage;
--   DROP INDEX IF EXISTS ix_artifacts_member_created_by, ix_artifact_shares_member_created_by;

CREATE TABLE IF NOT EXISTS artifact_member_usage (
  member_principal   text PRIMARY KEY,
  max_artifact_bytes bigint,
  max_member_bytes   bigint,
  max_artifacts      integer,
  bytes_used         bigint NOT NULL DEFAULT 0,
  bytes_retracted    bigint NOT NULL DEFAULT 0,
  sharing_disabled   boolean NOT NULL DEFAULT false,
  display_name       text,
  last_used_at       timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (bytes_used >= 0 AND bytes_retracted >= 0),
  CHECK (max_artifact_bytes IS NULL OR max_artifact_bytes > 0),
  CHECK (max_member_bytes IS NULL OR max_member_bytes > 0),
  CHECK (max_artifacts IS NULL OR max_artifacts > 0)
);

CREATE TABLE IF NOT EXISTS artifact_member_events (
  event_id         bigserial PRIMARY KEY,
  member_principal text NOT NULL,
  actor            jsonb NOT NULL,
  action           text NOT NULL,
  artifact_id      text,
  share_id         text,
  audience         text,
  outcome          text NOT NULL CHECK (outcome IN ('ok', 'refused', 'error')),
  code             text,
  detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
  at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_artifact_member_events_member ON artifact_member_events (member_principal, at DESC);
CREATE INDEX IF NOT EXISTS ix_artifact_member_events_audience ON artifact_member_events (audience, at DESC)
  WHERE share_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_artifacts_member_created_by ON artifacts (created_by) WHERE created_by LIKE 'member:%';
CREATE INDEX IF NOT EXISTS ix_artifact_shares_member_created_by ON artifact_shares (created_by)
  WHERE created_by LIKE 'member:%';

-- member.artifact_create idempotency: one row per (member, idempotency key), whatever actor (user or agent) sent it.
CREATE TABLE IF NOT EXISTS artifact_member_creates (
  member_principal text NOT NULL,
  idempotency_key  text NOT NULL,
  request_sha256   text NOT NULL,
  artifact_id      text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (member_principal, idempotency_key)
);
