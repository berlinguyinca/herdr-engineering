-- Immutable recovery generations and exact event-identity coverage. No journal
-- rows are changed or deleted. An offline builder publishes the active pointer
-- only after both the selected original rows and ALL covered keys are durable.
CREATE TABLE IF NOT EXISTS recovery_checkpoint_manifests (
    checkpoint_id text PRIMARY KEY,
    format_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    covered_events bigint NOT NULL DEFAULT 0 CHECK (covered_events >= 0),
    selected_events bigint NOT NULL DEFAULT 0 CHECK (selected_events >= 0),
    ready boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS recovery_checkpoint_rows (
    checkpoint_id text NOT NULL REFERENCES recovery_checkpoint_manifests ON DELETE CASCADE,
    actor_id text NOT NULL,
    event_id text NOT NULL,
    event_row jsonb NOT NULL,
    PRIMARY KEY (checkpoint_id, actor_id, event_id)
);

CREATE TABLE IF NOT EXISTS recovery_checkpoint_covered (
    checkpoint_id text NOT NULL REFERENCES recovery_checkpoint_manifests ON DELETE CASCADE,
    actor_id text NOT NULL,
    event_id text NOT NULL,
    entity_type text,
    entity_id text,
    PRIMARY KEY (checkpoint_id, actor_id, event_id)
);

CREATE TABLE IF NOT EXISTS recovery_checkpoint_active (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    checkpoint_id text NOT NULL REFERENCES recovery_checkpoint_manifests
);

-- INCLUDE(actor) lets PostgreSQL compute the expression from an index-only scan,
-- without fetching every journal heap row to read actor. The existing unique
-- expression index remains the authority for per-actor identity uniqueness.
-- On a large existing installation, build this before the transactional migrator:
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fabric_events_recovery_identity
--   ON fabric_events ((COALESCE(actor ->> 'id', '')), event_id) INCLUDE (actor);
-- Verify indisvalid/indisready if a concurrent index build was interrupted.
CREATE INDEX IF NOT EXISTS idx_fabric_events_recovery_identity
    ON fabric_events ((COALESCE(actor ->> 'id', '')), event_id) INCLUDE (actor);
