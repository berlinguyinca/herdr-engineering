-- Migration 0045 (0044 is assistant_reminders on main).
-- Collaborator MCP bridge: the intent ledger (every collaborator or user intent and its outcome), the verified-upload
-- table (the sole authority that a staged upload is usable), and invite grants (a read/write grant promised to an
-- agent that has not joined yet).  Additive and idempotent.  Safe to re-run.
--
-- DOWN:
--   ALTER TABLE artifact_collab_attempts DROP COLUMN IF EXISTS ticket_id;
--   DROP TABLE IF EXISTS artifact_collab_invite_grants;
--   DROP TABLE IF EXISTS artifact_collab_uploads;
--   DROP TABLE IF EXISTS artifact_collab_intents;

-- outcome 'received' means undecided.  The commit kind's outcomes are created, merged, conflict, rejected; other
-- kinds' handlers choose their own.  No FK on artifact_id: an intent may name an artifact that does not exist, and a
-- non-commit kind may name none.  bytes is inline text bytes only; staged bytes are counted in artifact_collab_uploads.
CREATE TABLE IF NOT EXISTS artifact_collab_intents (
  ticket_id       text PRIMARY KEY,
  kind            text NOT NULL DEFAULT 'commit',
  actor_kind      text NOT NULL CHECK (actor_kind IN ('agent', 'user')),
  actor_id        text NOT NULL,
  artifact_id     text,
  base_version_id text,
  idempotency_key text,
  request_sha256  text,
  bytes           bigint NOT NULL DEFAULT 0,
  outcome         text NOT NULL CHECK (outcome ~ '^[a-z_]{1,32}$'),
  version_id      text,
  attempt_id      text,
  reason          text,
  received_at     timestamptz NOT NULL DEFAULT now(),
  decided_at      timestamptz
);

CREATE INDEX IF NOT EXISTS artifact_collab_intents_artifact_idx
  ON artifact_collab_intents (artifact_id, received_at DESC);
CREATE INDEX IF NOT EXISTS artifact_collab_intents_actor_idx
  ON artifact_collab_intents (actor_kind, actor_id, received_at DESC);
CREATE INDEX IF NOT EXISTS artifact_collab_intents_key_idx
  ON artifact_collab_intents (actor_kind, actor_id, kind, idempotency_key);

-- Written only by the worker after put_stream verified the bytes.  owner_key is the staging owner (the agent id for
-- collaborators).  charged is true only for uploads counted against the per-agent daily quota (commit uploads).
CREATE TABLE IF NOT EXISTS artifact_collab_uploads (
  owner_key    text NOT NULL,
  upload_id    text NOT NULL,
  artifact_id  text NOT NULL,
  name         text NOT NULL,
  sha256       text NOT NULL,
  size_bytes   bigint NOT NULL,
  storage_key  text NOT NULL,
  media_type   text NOT NULL,
  charged      boolean NOT NULL,
  verified_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_key, upload_id)
);

CREATE TABLE IF NOT EXISTS artifact_collab_invite_grants (
  invite_id         text PRIMARY KEY,
  artifact_id       text NOT NULL REFERENCES artifacts (artifact_id),
  permission        text NOT NULL CHECK (permission IN ('read', 'write')),
  expires_at        timestamptz NOT NULL,
  granted_by        text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  resolved_agent_id text,
  resolved_at       timestamptz
);

ALTER TABLE artifact_collab_attempts ADD COLUMN IF NOT EXISTS ticket_id text;
