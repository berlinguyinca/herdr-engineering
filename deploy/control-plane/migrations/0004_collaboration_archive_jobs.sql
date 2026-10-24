-- Durable checkpoints for archive export, verified cleanup and privacy rewrites.
-- Payloads are temporary recovery data, removed when the operation completes.
CREATE TABLE collab_lifecycle_jobs (
    job_id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('archive', 'scrub', 'rewrite', 'restore')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
