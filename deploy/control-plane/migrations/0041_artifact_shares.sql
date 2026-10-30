-- Artifact share links (docs/superpowers/specs/2026-10-30-artifact-share-links-design.md): one row per share of ONE
-- pinned artifact version, plus an append-only audit trail.  Only sha256(token) is stored, and only once the share goes
-- live (token_hash is NULL while pending); the token itself never reaches the database.  The internet-facing share
-- container has no database access: it appends audit lines to a file that the worker ingests into
-- artifact_share_events (ingest_key makes that ingestion idempotent).  State machine: pending -> live -> revoked.
-- Additive and idempotent.  Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS artifact_share_events, artifact_shares;

CREATE TABLE IF NOT EXISTS artifact_shares (
  share_id     text PRIMARY KEY,
  artifact_id  text NOT NULL REFERENCES artifacts (artifact_id),
  version_id   text NOT NULL REFERENCES artifact_versions (version_id),
  audience     text NOT NULL CHECK (audience IN ('lab', 'public')),
  token_hash   text CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  allow_emails text[] NOT NULL DEFAULT '{}',
  allow_groups text[] NOT NULL DEFAULT '{}',
  state        text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'live', 'revoked')),
  entry        text,
  file_count   integer,
  total_bytes  bigint,
  title        text,
  expires_at   timestamptz,
  created_by   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  approved_by  text,
  approved_at  timestamptz,
  revoked_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifact_shares_token_hash ON artifact_shares (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_artifact_shares_artifact ON artifact_shares (artifact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_artifact_shares_live_expiry ON artifact_shares (expires_at) WHERE state = 'live';

CREATE TABLE IF NOT EXISTS artifact_share_events (
  event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  share_id   text NOT NULL REFERENCES artifact_shares (share_id),
  at         timestamptz NOT NULL DEFAULT now(),
  kind       text NOT NULL CHECK (kind IN ('created', 'approved', 'opened', 'denied', 'revoked', 'expired')),
  actor      text NOT NULL,
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  ingest_key text
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_artifact_share_events_ingest ON artifact_share_events (ingest_key) WHERE ingest_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_artifact_share_events_share ON artifact_share_events (share_id, at, event_id);
