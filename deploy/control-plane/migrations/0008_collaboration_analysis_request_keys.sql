-- Preserve historical immutable records: a caller key cannot be invented for them.
ALTER TABLE collab_analysis_requests
    ADD COLUMN creator_key text,
    ADD COLUMN request_key text,
    ADD COLUMN request_fingerprint text,
    ADD CONSTRAINT collab_analysis_request_key_valid CHECK (
        num_nonnulls(creator_key, request_key, request_fingerprint) = 0 OR (
            num_nonnulls(creator_key, request_key, request_fingerprint) = 3
            AND length(btrim(request_key)) BETWEEN 1 AND 200
            AND request_fingerprint ~ '^[0-9a-f]{64}$'
            AND (creator_key = record->>'requested_by') IS TRUE
            AND (request_key = record->>'request_key') IS TRUE)),
    ADD CONSTRAINT collab_analysis_request_creator_key_unique UNIQUE (creator_key, request_key);
