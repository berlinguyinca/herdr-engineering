-- Safe to re-run: every statement is guarded (IF NOT EXISTS / OR REPLACE / DROP .. IF EXISTS).
--
-- DOWN:
--   DROP TABLE IF EXISTS collab_skill_drafts CASCADE;  -- plus the collab_skill_* trigger functions and the added collab_skill_versions columns
-- Upgrade both the original 0003 and the briefly amended registry schema.
-- Keep unsigned legacy object references for cleanup, but revoke their availability.
-- Drafts store metadata and an immutable S3 reference, never generated code bytes.
CREATE TABLE IF NOT EXISTS collab_skill_drafts (
    draft_id text PRIMARY KEY,
    skill_id text NOT NULL REFERENCES collab_skills(skill_id) ON DELETE CASCADE,
    created_by text NOT NULL REFERENCES collab_users(user_id) ON DELETE CASCADE,
    -- Publication target hint, not live ownership: private packages survive room deletion.
    target_room_id text,
    version text NOT NULL,
    storage_key text NOT NULL UNIQUE,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    signature text NOT NULL,
    key_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);


ALTER TABLE collab_skill_drafts
    ADD COLUMN IF NOT EXISTS request_key text,
    ADD COLUMN IF NOT EXISTS request_fingerprint text,
    ADD COLUMN IF NOT EXISTS storage_bucket text;
ALTER TABLE collab_skill_drafts DROP CONSTRAINT IF EXISTS collab_skill_draft_request_check;
ALTER TABLE collab_skill_drafts ADD CONSTRAINT collab_skill_draft_request_check CHECK (
    (request_key IS NULL AND request_fingerprint IS NULL) OR
    (request_key IS NOT NULL AND length(request_key) BETWEEN 1 AND 200
     AND request_fingerprint IS NOT NULL AND request_fingerprint ~ '^[0-9a-f]{64}$'));
CREATE UNIQUE INDEX IF NOT EXISTS collab_skill_draft_request
    ON collab_skill_drafts(created_by, request_key) WHERE request_key IS NOT NULL;

ALTER TABLE collab_skill_versions
    ADD COLUMN IF NOT EXISTS manifest jsonb CHECK (jsonb_typeof(manifest) = 'object'),
    ADD COLUMN IF NOT EXISTS signature text,
    ADD COLUMN IF NOT EXISTS key_id text,
    ADD COLUMN IF NOT EXISTS source_draft_id text UNIQUE REFERENCES collab_skill_drafts(draft_id),
    ADD COLUMN IF NOT EXISTS storage_bucket text,
    ADD COLUMN IF NOT EXISTS legacy_unverified boolean NOT NULL DEFAULT false;
DROP TRIGGER IF EXISTS collab_skill_version_immutable ON collab_skill_versions;
ALTER TABLE collab_skill_versions DROP CONSTRAINT IF EXISTS collab_skill_versions_check;
UPDATE collab_skill_versions SET legacy_unverified=true, status='revoked'
WHERE manifest IS NULL OR signature IS NULL OR key_id IS NULL OR source_draft_id IS NULL;
-- Never fabricate a signature, manifest, bucket, or idempotency key for legacy data.
ALTER TABLE collab_skill_versions ADD CONSTRAINT collab_skill_versions_check CHECK (
    (legacy_unverified AND status = 'revoked') OR
    (NOT legacy_unverified AND (status = 'draft' OR (
        manifest IS NOT NULL AND signature IS NOT NULL AND key_id IS NOT NULL
        AND source_draft_id IS NOT NULL
        AND signature ~ '^[0-9a-f]{128}$' AND length(key_id) > 0
        AND (manifest->>'schema_version' = '1') IS TRUE
        AND (manifest->>'skill_id' = skill_id) IS TRUE
        AND (manifest->>'version' = version) IS TRUE))));

-- Published package identity/content is immutable; revocation is terminal.
-- Parent deletion remains available for the existing privacy/scrub lifecycle.
CREATE OR REPLACE FUNCTION collab_skill_version_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status != 'draft' AND EXISTS (
            SELECT 1 FROM collab_skills WHERE skill_id = OLD.skill_id) THEN
            RAISE EXCEPTION 'published skill versions cannot be deleted' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status != 'draft' AND (
        (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status')
        OR NEW.status NOT IN ('published', 'revoked')
        OR (OLD.status = 'revoked' AND NEW.status != 'revoked')) THEN
        RAISE EXCEPTION 'published skill versions are immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS collab_skill_version_immutable ON collab_skill_versions;
CREATE TRIGGER collab_skill_version_immutable BEFORE UPDATE OR DELETE ON collab_skill_versions
    FOR EACH ROW EXECUTE FUNCTION collab_skill_version_immutable();

CREATE OR REPLACE FUNCTION collab_skill_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.skill_id, NEW.owner_id, NEW.scope, NEW.room_id) IS DISTINCT FROM
       (OLD.skill_id, OLD.owner_id, OLD.scope, OLD.room_id)
       AND EXISTS (SELECT 1 FROM collab_skill_versions
                   WHERE skill_id = OLD.skill_id AND status != 'draft') THEN
        RAISE EXCEPTION 'published skill identity and scope are immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS collab_skill_identity_immutable ON collab_skills;
DROP TRIGGER IF EXISTS collab_skill_identity_immutable ON collab_skills;
CREATE TRIGGER collab_skill_identity_immutable BEFORE UPDATE ON collab_skills
    FOR EACH ROW EXECUTE FUNCTION collab_skill_identity_immutable();

CREATE OR REPLACE FUNCTION collab_skill_draft_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'skill drafts are immutable; create a new draft' USING ERRCODE = '23514';
END;
$$;
DROP TRIGGER IF EXISTS collab_skill_draft_immutable ON collab_skill_drafts;
DROP TRIGGER IF EXISTS collab_skill_draft_immutable ON collab_skill_drafts;
CREATE TRIGGER collab_skill_draft_immutable BEFORE UPDATE ON collab_skill_drafts
    FOR EACH ROW EXECUTE FUNCTION collab_skill_draft_immutable();
