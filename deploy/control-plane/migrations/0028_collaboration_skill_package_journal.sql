-- Safe to re-run: every statement is guarded (IF NOT EXISTS / OR REPLACE / DROP .. IF EXISTS).
--
-- DOWN:
--   DROP TABLE IF EXISTS collab_skill_package_journal;
-- Commit ownership on an independent connection BEFORE any package PUT.
-- No parent FK: failed publication and user/skill cascades must not lose bytes.
-- Keep committed packages here too, until verified user erasure removes them.
CREATE TABLE IF NOT EXISTS collab_skill_package_journal (
    creator_id text NOT NULL,
    storage_bucket text NOT NULL CHECK (length(storage_bucket) > 0),
    storage_key text NOT NULL CHECK (length(storage_key) > 0),
    request_key text NOT NULL CHECK (length(request_key) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (creator_id, storage_bucket, storage_key)
);
