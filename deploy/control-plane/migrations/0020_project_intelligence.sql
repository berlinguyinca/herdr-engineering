-- Project intelligence (spec herdr-project-intelligence-autonomy-v1, P1). Safe to re-run.
--
-- The event log stays fabric_events (envelope v2); this adds only the connector inbox/outbox and the
-- intelligence entities. Inferred relationships live here, never in the lineage graph (DECISIONS D1).
-- Every table carries org_id + security_label; reads filter on both plus the project ACL.
--
-- DOWN:
--   DROP TABLE IF EXISTS pi_audit, pi_finding, pi_relationship, pi_outbox, pi_event, pi_source_state, pi_record;
-- (Drops only tables created here. fabric_events and artifacts are untouched.)

CREATE TABLE IF NOT EXISTS pi_record (
    org_id         text        NOT NULL,
    kind           text        NOT NULL CHECK (kind IN ('project', 'investigation')),
    record_id      text        NOT NULL,
    name           text        NOT NULL,
    project_ids    text[]      NOT NULL DEFAULT '{}',
    owner          text,
    status         text        NOT NULL DEFAULT 'active',
    security_label text        NOT NULL DEFAULT 'internal'
                   CHECK (security_label IN ('public', 'internal', 'confidential', 'restricted')),
    version        integer     NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, kind, record_id)
);

-- Connector cursor/health per source (cursor is stored only after the durable transaction).
CREATE TABLE IF NOT EXISTS pi_source_state (
    org_id        text        NOT NULL,
    source_system text        NOT NULL,
    source_instance text      NOT NULL,
    cursor        text,
    lag_seconds   integer,
    failure_count integer     NOT NULL DEFAULT 0,
    paused        boolean     NOT NULL DEFAULT false,
    last_health   text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, source_system, source_instance)
);

-- Normalized events. event_id is derived from the dedupe key, so replay is a no-op.
CREATE TABLE IF NOT EXISTS pi_event (
    event_id        text        PRIMARY KEY,
    org_id          text        NOT NULL,
    source_system   text        NOT NULL,
    source_instance text        NOT NULL,
    source_id       text        NOT NULL,
    source_version  text        NOT NULL DEFAULT '',
    event_type      text        NOT NULL,
    occurred_at     timestamptz NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    project_ids     text[]      NOT NULL DEFAULT '{}',
    investigation_ids text[]    NOT NULL DEFAULT '{}',
    object_ref      text        NOT NULL,
    actor_ref       text,
    title           text        NOT NULL,
    category        text        NOT NULL DEFAULT 'event',
    evidence_refs   jsonb       NOT NULL DEFAULT '[]',
    security_label  text        NOT NULL DEFAULT 'internal'
                    CHECK (security_label IN ('public', 'internal', 'confidential', 'restricted')),
    visibility_scope text       NOT NULL DEFAULT 'private',
    payload_schema_version integer NOT NULL DEFAULT 1,
    payload         jsonb       NOT NULL DEFAULT '{}',
    content_digest  text        NOT NULL,
    correlation_id  text,
    causation_id    text,
    supersedes_id   text        REFERENCES pi_event (event_id),
    late            boolean     NOT NULL DEFAULT false,
    tombstoned      boolean     NOT NULL DEFAULT false,
    UNIQUE (org_id, source_system, source_instance, source_id, source_version, event_type)
);
CREATE INDEX IF NOT EXISTS pi_event_org_time ON pi_event (org_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS pi_event_projects ON pi_event USING gin (project_ids);
CREATE INDEX IF NOT EXISTS pi_event_object ON pi_event (org_id, object_ref);

-- Transactional outbox: one row per (event, consumer); consumers ack idempotently.
CREATE TABLE IF NOT EXISTS pi_outbox (
    event_id  text        NOT NULL REFERENCES pi_event (event_id) ON DELETE CASCADE,
    consumer  text        NOT NULL,
    done      boolean     NOT NULL DEFAULT false,
    attempts  integer     NOT NULL DEFAULT 0,
    dead      boolean     NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer)
);

CREATE TABLE IF NOT EXISTS pi_relationship (
    id             text        PRIMARY KEY,
    org_id         text        NOT NULL,
    project_ids    text[]      NOT NULL DEFAULT '{}',
    from_ref       text        NOT NULL,
    to_ref         text        NOT NULL,
    predicate      text        NOT NULL,
    derivation     text        NOT NULL CHECK (derivation IN ('explicit', 'inferred', 'human_verified', 'system_verified')),
    evidence_refs  jsonb       NOT NULL CHECK (jsonb_array_length(evidence_refs) > 0),
    security_label text        NOT NULL DEFAULT 'internal'
                   CHECK (security_label IN ('public', 'internal', 'confidential', 'restricted')),
    review_state   text        NOT NULL DEFAULT 'provisional' CHECK (review_state IN ('provisional', 'confirmed', 'rejected')),
    confidence     numeric(4, 3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    extractor_version text,
    model_id       text,
    verified_by    text,
    superseded_by  text,
    version        integer     NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (derivation <> 'inferred' OR (confidence IS NOT NULL AND extractor_version IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS pi_relationship_from ON pi_relationship (org_id, from_ref);
CREATE INDEX IF NOT EXISTS pi_relationship_to ON pi_relationship (org_id, to_ref);

CREATE TABLE IF NOT EXISTS pi_finding (
    id              text        PRIMARY KEY,
    org_id          text        NOT NULL,
    project_ids     text[]      NOT NULL DEFAULT '{}',
    statement       text        NOT NULL,
    evidence_refs   jsonb       NOT NULL DEFAULT '[]',
    security_label  text        NOT NULL DEFAULT 'internal',
    extractor_version text      NOT NULL,
    model_identifier text       NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit of reviews, pauses, backfill requests and automation decisions. No message bodies.
CREATE TABLE IF NOT EXISTS pi_audit (
    id         bigserial   PRIMARY KEY,
    org_id     text        NOT NULL,
    actor      text        NOT NULL,
    action     text        NOT NULL,
    subject    text,
    detail     jsonb       NOT NULL DEFAULT '{}',
    at         timestamptz NOT NULL DEFAULT now()
);
