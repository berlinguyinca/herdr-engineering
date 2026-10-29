-- Safe to re-run: every statement is guarded (IF NOT EXISTS / OR REPLACE / DROP .. IF EXISTS).
--
-- DOWN:
--   DROP TABLE IF EXISTS collab_audit_events, collab_skill_installations, collab_skill_versions, collab_skills,
--     collab_room_archives, collab_room_files, collab_room_upload_parts, collab_room_uploads, collab_session_logs,
--     collab_messages, collab_agent_access_requests, collab_agent_permissions, collab_agent_room_attachments,
--     collab_agents, collab_room_invitations, collab_room_memberships, collab_rooms, collab_users CASCADE;
-- Collaboration's relational boundary. Prefixes avoid the fabric's agents and
-- audit_events tables. Existing room_/agent_/membership_ IDs and identity-provider
-- user IDs are text, not UUIDs. This migration does not rewrite fabric_events.
CREATE TABLE IF NOT EXISTS collab_users (
    user_id text PRIMARY KEY,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'disabled', 'kicked')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collab_rooms (
    room_id text PRIMARY KEY,
    project_id text,
    name text NOT NULL,
    owner_id text REFERENCES collab_users(user_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archiving', 'deleted')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collab_room_memberships (
    membership_id text UNIQUE,
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES collab_users(user_id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'removed')),
    role text NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner', 'contributor', 'viewer', 'guest')),
    can_upload boolean NOT NULL DEFAULT false,
    -- Epoch seconds match CollabService, including legacy non-expiring grants.
    expires_at double precision CHECK (expires_at > '-Infinity'::float8 AND expires_at < 'Infinity'::float8),
    joined_seq bigint NOT NULL DEFAULT 0 CHECK (joined_seq >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, user_id)
);

CREATE TABLE IF NOT EXISTS collab_room_invitations (
    invitation_id text PRIMARY KEY,
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES collab_users(user_id) ON DELETE CASCADE,
    invited_by text REFERENCES collab_users(user_id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS collab_room_invitations_pending
    ON collab_room_invitations(room_id, user_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS collab_agents (
    agent_id text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES collab_users(user_id) ON DELETE CASCADE,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS collab_agent_room_attachments (
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES collab_agents(agent_id) ON DELETE CASCADE,
    actions text[] NOT NULL DEFAULT '{}' CHECK (
        actions <@ ARRAY['read', 'post', 'instruct', 'execute', 'spawn_helpers']::text[]
        AND array_position(actions, NULL) IS NULL),
    expires_at double precision CHECK (expires_at > '-Infinity'::float8 AND expires_at < 'Infinity'::float8),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'revoked')),
    granted_seq bigint NOT NULL DEFAULT 0 CHECK (granted_seq >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, agent_id)
);
CREATE TABLE IF NOT EXISTS collab_agent_permissions (
    permission_id text UNIQUE,
    room_id text NOT NULL,
    agent_id text NOT NULL,
    user_id text NOT NULL,
    status text NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, agent_id, user_id),
    FOREIGN KEY (room_id, agent_id) REFERENCES collab_agent_room_attachments ON DELETE CASCADE,
    FOREIGN KEY (room_id, user_id) REFERENCES collab_room_memberships ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS collab_agent_access_requests (
    request_id text PRIMARY KEY,
    room_id text NOT NULL,
    agent_id text NOT NULL,
    user_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (room_id, agent_id) REFERENCES collab_agent_room_attachments ON DELETE CASCADE,
    FOREIGN KEY (room_id, user_id) REFERENCES collab_room_memberships ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS collab_agent_access_requests_pending
    ON collab_agent_access_requests(room_id, agent_id, user_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS collab_messages (
    message_id text PRIMARY KEY,
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    author_user_id text REFERENCES collab_users(user_id) ON DELETE CASCADE,
    author_agent_id text REFERENCES collab_agents(agent_id) ON DELETE CASCADE,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(author_user_id, author_agent_id) = 1)
);
CREATE INDEX IF NOT EXISTS collab_messages_room ON collab_messages(room_id, created_at, message_id);
CREATE TABLE IF NOT EXISTS collab_session_logs (
    log_id text PRIMARY KEY,
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    session_id text,
    author_user_id text REFERENCES collab_users(user_id) ON DELETE CASCADE,
    author_agent_id text REFERENCES collab_agents(agent_id) ON DELETE CASCADE,
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(author_user_id, author_agent_id) = 1)
);

CREATE TABLE IF NOT EXISTS collab_room_uploads (
    upload_id text PRIMARY KEY,
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    created_by text REFERENCES collab_users(user_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'uploading', 'verifying', 'available', 'failed')),
    manifest jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(manifest) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (room_id, upload_id)
);
CREATE TABLE IF NOT EXISTS collab_room_upload_parts (
    upload_id text NOT NULL REFERENCES collab_room_uploads(upload_id) ON DELETE CASCADE,
    relative_path text NOT NULL,
    part_number int NOT NULL CHECK (part_number > 0),
    multipart_id text,
    etag text,
    size_bytes bigint NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    sha256 text CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'uploading', 'verifying', 'available', 'failed')),
    PRIMARY KEY (upload_id, relative_path, part_number)
);
CREATE TABLE IF NOT EXISTS collab_room_files (
    file_id text PRIMARY KEY,
    room_id text NOT NULL REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    upload_id text,
    uploaded_by text REFERENCES collab_users(user_id) ON DELETE SET NULL,
    relative_path text NOT NULL,
    storage_key text NOT NULL UNIQUE,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256 text CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'verifying' CHECK (status IN ('verifying', 'available', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status != 'available' OR sha256 IS NOT NULL),
    FOREIGN KEY (room_id, upload_id) REFERENCES collab_room_uploads(room_id, upload_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS collab_room_files_room ON collab_room_files(room_id, relative_path, file_id);

-- Archive provenance is deliberately NOT a live-room FK. The catalog must
-- survive verified deletion. Object verification/scrubbing belongs to the
-- archive service; SQL deletion alone does not authorize deleting a live room.
CREATE TABLE IF NOT EXISTS collab_room_archives (
    archive_id text PRIMARY KEY,
    original_room_id text NOT NULL,
    live_room_id text REFERENCES collab_rooms(room_id) ON DELETE SET NULL,
    requested_by text REFERENCES collab_users(user_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'archiving' CHECK (status IN ('archiving', 'verified', 'failed')),
    manifest_key text UNIQUE,
    manifest_sha256 text CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status != 'verified' OR (manifest_key IS NOT NULL AND manifest_sha256 IS NOT NULL AND verified_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS collab_room_archives_original ON collab_room_archives(original_room_id);

CREATE TABLE IF NOT EXISTS collab_skills (
    skill_id text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES collab_users(user_id) ON DELETE CASCADE,
    room_id text REFERENCES collab_rooms(room_id) ON DELETE CASCADE,
    name text NOT NULL,
    scope text NOT NULL DEFAULT 'private' CHECK (scope IN ('private', 'room', 'shared')),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope = 'room') = (room_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS collab_skill_versions (
    skill_id text NOT NULL REFERENCES collab_skills(skill_id) ON DELETE CASCADE,
    version text NOT NULL,
    storage_key text NOT NULL UNIQUE,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (skill_id, version)
);
CREATE TABLE IF NOT EXISTS collab_skill_installations (
    installation_id text PRIMARY KEY,
    skill_id text NOT NULL,
    version text NOT NULL,
    agent_id text NOT NULL REFERENCES collab_agents(agent_id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES collab_users(user_id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'installed' CHECK (status IN ('installed', 'removed', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (skill_id, version, agent_id),
    FOREIGN KEY (skill_id, version) REFERENCES collab_skill_versions ON DELETE CASCADE
);

-- Minimal security tombstones only: no message text, identities, or credentials.
CREATE TABLE IF NOT EXISTS collab_audit_events (
    event_id text PRIMARY KEY,
    actor_id text REFERENCES collab_users(user_id) ON DELETE SET NULL,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    -- Immutable creation-request digest, not a second copy of scrubbed content.
    request_fingerprint text CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS collab_audit_events_entity ON collab_audit_events(entity_type, entity_id, occurred_at);
