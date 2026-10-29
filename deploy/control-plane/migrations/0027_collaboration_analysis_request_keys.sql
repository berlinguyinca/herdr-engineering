-- Safe to re-run: every statement is guarded (IF NOT EXISTS / OR REPLACE / DROP .. IF EXISTS).
--
-- DOWN:
--   ALTER TABLE collab_analysis_requests DROP CONSTRAINT IF EXISTS collab_analysis_request_creator_key_unique,
--     DROP CONSTRAINT IF EXISTS collab_analysis_request_key_valid, DROP COLUMN IF EXISTS creator_key,
--     DROP COLUMN IF EXISTS request_key, DROP COLUMN IF EXISTS request_fingerprint;
-- Preserve historical immutable records: a caller key cannot be invented for them.
ALTER TABLE collab_analysis_requests
    ADD COLUMN IF NOT EXISTS creator_key text,
    ADD COLUMN IF NOT EXISTS request_key text,
    ADD COLUMN IF NOT EXISTS request_fingerprint text;
ALTER TABLE collab_analysis_requests
    DROP CONSTRAINT IF EXISTS collab_analysis_request_key_valid,
    DROP CONSTRAINT IF EXISTS collab_analysis_request_creator_key_unique;
ALTER TABLE collab_analysis_requests
    ADD CONSTRAINT collab_analysis_request_key_valid CHECK (
        num_nonnulls(creator_key, request_key, request_fingerprint) = 0 OR (
            num_nonnulls(creator_key, request_key, request_fingerprint) = 3
            AND length(btrim(request_key)) BETWEEN 1 AND 200
            AND request_fingerprint ~ '^[0-9a-f]{64}$'
            AND (creator_key = record->>'requested_by') IS TRUE
            AND (request_key = record->>'request_key') IS TRUE)),
    ADD CONSTRAINT collab_analysis_request_creator_key_unique UNIQUE (creator_key, request_key);
