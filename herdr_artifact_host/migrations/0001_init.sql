-- Artifact host schema (standalone; member-owned artifacts only).  Derived from control-plane migrations
-- 0041, 0045-0051 minus CI/agent report columns.  Applied by herdr_artifact_host.db.Migrator.

CREATE TYPE intake_state AS ENUM ('accepted', 'pending', 'discarded');

CREATE TABLE artifact_blobs (
  sha256     text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  media_type text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE artifacts (
  artifact_id       text PRIMARY KEY,
  owner             text NOT NULL,                       -- member id
  original_filename text,
  display_name      text,
  artifact_type     text NOT NULL DEFAULT 'other',
  media_type        text,
  size_bytes        bigint NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
  intake            intake_state NOT NULL DEFAULT 'accepted',
  lifecycle         text NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'archived', 'tombstoned')),
  legal_hold        boolean NOT NULL DEFAULT false,
  current_version_id text,
  tombstoned_at     timestamptz,
  purge_after       timestamptz,
  created_by        text,                                -- principal: member:<id> or agent:<id>
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  search_text       text GENERATED ALWAYS AS
    (lower(coalesce(display_name, '') || ' ' || coalesce(original_filename, '') || ' ' || artifact_type)) STORED
);
CREATE INDEX ix_artifacts_owner_feed ON artifacts (owner, created_at DESC, artifact_id);
CREATE INDEX ix_artifacts_pending_intake ON artifacts (owner) WHERE intake = 'pending';
CREATE INDEX ix_artifacts_purge_due ON artifacts (purge_after) WHERE lifecycle = 'tombstoned';
CREATE INDEX ix_artifacts_fts ON artifacts USING gin (to_tsvector('simple', search_text));

CREATE TABLE artifact_versions (
  version_id          text PRIMARY KEY,
  artifact_id         text NOT NULL REFERENCES artifacts (artifact_id) ON DELETE CASCADE,
  version_number      int NOT NULL CHECK (version_number > 0),
  parent_version_ids  text[] NOT NULL DEFAULT '{}',
  state               text NOT NULL DEFAULT 'ready' CHECK (state IN ('draft', 'ready', 'purged')),
  output_manifest     jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{name, sha256, size_bytes, media_type}]
  provenance          jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_via         text NOT NULL DEFAULT 'owner' CHECK (created_via IN ('owner', 'collaborator')),
  collaborator_agent_id text,
  created_by          text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  purged_at           timestamptz,
  UNIQUE (artifact_id, version_number),
  UNIQUE (version_id, artifact_id),
  CHECK ((created_via = 'owner' AND collaborator_agent_id IS NULL)
      OR (created_via = 'collaborator' AND collaborator_agent_id IS NOT NULL))
);
CREATE INDEX ix_artifact_versions_artifact ON artifact_versions (artifact_id, version_number DESC);
ALTER TABLE artifacts ADD CONSTRAINT fk_artifacts_current_version
  FOREIGN KEY (current_version_id) REFERENCES artifact_versions (version_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE artifact_acl (
  artifact_id  text NOT NULL REFERENCES artifacts (artifact_id) ON DELETE CASCADE,
  principal_id text NOT NULL,
  permissions  text[] NOT NULL DEFAULT '{read}' CHECK (permissions <@ ARRAY['read', 'write', 'admin']),
  expires_at   timestamptz,
  granted_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (artifact_id, principal_id)
);
CREATE INDEX ix_artifact_acl_principal ON artifact_acl (principal_id, expires_at);

CREATE TABLE library_folders (
  folder_id  text PRIMARY KEY CHECK (folder_id ~ '^fld_[0-9a-f]{24}$'),
  owner      text NOT NULL,
  name       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_library_folders_owner_name ON library_folders (owner, lower(name));

CREATE TABLE library_items (
  folder_id   text NOT NULL REFERENCES library_folders (folder_id) ON DELETE CASCADE,
  artifact_id text NOT NULL REFERENCES artifacts (artifact_id) ON DELETE CASCADE,
  source      text NOT NULL CHECK (source IN ('manual', 'suggested', 'auto')),
  filed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (folder_id, artifact_id)
);
CREATE INDEX ix_library_items_artifact ON library_items (artifact_id);

CREATE TABLE shares (
  share_id     text PRIMARY KEY,
  artifact_id  text NOT NULL REFERENCES artifacts (artifact_id) ON DELETE CASCADE,
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
CREATE UNIQUE INDEX ux_shares_token_hash ON shares (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX ix_shares_artifact ON shares (artifact_id, created_at DESC);
CREATE INDEX ix_shares_live_expiry ON shares (expires_at) WHERE state = 'live';
CREATE INDEX ix_shares_created_by ON shares (created_by);

CREATE TABLE share_events (
  event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  share_id   text NOT NULL REFERENCES shares (share_id) ON DELETE CASCADE,
  at         timestamptz NOT NULL DEFAULT now(),
  kind       text NOT NULL CHECK (kind IN ('created', 'approved', 'opened', 'denied', 'revoked', 'expired',
    'claimed', 'annotated', 'annotation_edited', 'annotation_withdrawn', 'annotation_dismissed',
    'annotation_addressed')),
  actor      text NOT NULL,
  detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
  ingest_key text
);
CREATE UNIQUE INDEX ux_share_events_ingest ON share_events (ingest_key) WHERE ingest_key IS NOT NULL;
CREATE INDEX ix_share_events_share ON share_events (share_id, at, event_id);

CREATE TABLE share_claims (
  member_id  text NOT NULL,
  share_id   text NOT NULL REFERENCES shares (share_id) ON DELETE CASCADE,
  title      text,
  sharer     text,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (member_id, share_id)
);
CREATE INDEX ix_share_claims_member ON share_claims (member_id, claimed_at DESC);

CREATE TABLE annotations (
  annotation_id           text PRIMARY KEY CHECK (annotation_id ~ '^ann_[A-Za-z0-9_-]{22}$'),
  artifact_id             text NOT NULL REFERENCES artifacts (artifact_id) ON DELETE CASCADE,
  version_id              text NOT NULL REFERENCES artifact_versions (version_id),
  share_id                text NOT NULL REFERENCES shares (share_id) ON DELETE CASCADE,
  file_name               text NOT NULL,
  anchor                  jsonb NOT NULL,
  body                    text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
  author_identity         text NOT NULL,
  author_email            text NOT NULL,
  author_role             text NOT NULL CHECK (author_role IN ('member', 'owner')),
  status                  text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'addressed', 'dismissed', 'withdrawn')),
  addressed_in_version_id text REFERENCES artifact_versions (version_id),
  resolved_by             text,
  resolved_at             timestamptz,
  edited_at               timestamptz,
  withdrawn_at            timestamptz,
  created_at              timestamptz NOT NULL,
  ingested_at             timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  ingest_key              text UNIQUE,
  CHECK ((status = 'addressed') = (addressed_in_version_id IS NOT NULL))
);
CREATE INDEX ix_annotations_artifact ON annotations (artifact_id, status, created_at DESC, annotation_id);
CREATE INDEX ix_annotations_author ON annotations (share_id, author_identity, created_at DESC);
CREATE INDEX ix_annotations_author_day ON annotations (author_identity, ingested_at);

CREATE TABLE member_usage (
  member_id          text PRIMARY KEY,
  max_artifact_bytes bigint CHECK (max_artifact_bytes IS NULL OR max_artifact_bytes > 0),
  max_member_bytes   bigint CHECK (max_member_bytes IS NULL OR max_member_bytes > 0),
  max_artifacts      integer CHECK (max_artifacts IS NULL OR max_artifacts > 0),
  max_pending        integer CHECK (max_pending IS NULL OR max_pending > 0),
  bytes_used         bigint NOT NULL DEFAULT 0 CHECK (bytes_used >= 0),
  bytes_retracted    bigint NOT NULL DEFAULT 0 CHECK (bytes_retracted >= 0),
  sharing_disabled   boolean NOT NULL DEFAULT false,
  display_name       text,
  last_used_at       timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_tokens (
  digest     text PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{64}$'),
  member_id  text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);
CREATE INDEX ix_agent_tokens_member ON agent_tokens (member_id);
