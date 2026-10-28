-- Durable projection for cross-harness work transfer. Event history remains
-- authoritative; this table makes dashboard queries and idempotent sync cheap.
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id text PRIMARY KEY,
    schema_version integer NOT NULL DEFAULT 1,
    state text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    created_by text NOT NULL,
    source jsonb NOT NULL,
    target jsonb NOT NULL,
    goal text NOT NULL,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    mission_id text,
    room_id text,
    idempotency_key text,
    UNIQUE (created_by, idempotency_key)
);

CREATE INDEX IF NOT EXISTS handoffs_state_updated_idx ON handoffs (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS handoffs_source_session_idx ON handoffs ((source->>'session_id'));
CREATE INDEX IF NOT EXISTS handoffs_target_session_idx ON handoffs ((target->>'session_id'));
