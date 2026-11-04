-- Canonical memory journal; never copies or rewrites the assistant snapshot.
-- Existing 0014 rows remain legacy assistant-scoped; no invented author IDs.
ALTER TABLE pa_memory ADD COLUMN IF NOT EXISTS domain_type text NOT NULL DEFAULT 'assistant'
    CHECK (domain_type IN ('assistant', 'project', 'personal'));
ALTER TABLE pa_memory ADD COLUMN IF NOT EXISTS domain_id text;

ALTER TABLE pa_memory ADD COLUMN IF NOT EXISTS provenance jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pa_memory ADD COLUMN IF NOT EXISTS capture_kind text NOT NULL DEFAULT 'fact'
    CHECK (capture_kind IN ('summary', 'fact'));
ALTER TABLE pa_memory ADD COLUMN IF NOT EXISTS approval_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pa_memory ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE TABLE IF NOT EXISTS pa_memory_revision (
    memory_id text NOT NULL REFERENCES pa_memory(memory_id),
    revision integer NOT NULL CHECK (revision > 0),
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    assistant_id text NOT NULL,
    domain_type text NOT NULL DEFAULT 'assistant' CHECK (domain_type IN ('assistant', 'project', 'personal')),
    domain_id text,
    operation text NOT NULL CHECK (operation IN ('RETAIN', 'DELETE')),
    content text NOT NULL,
    provenance jsonb NOT NULL,
    capture_kind text NOT NULL CHECK (capture_kind IN ('summary', 'fact')),
    approval_metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, revision)
);
-- One coalescing outbox slot per document. History remains in the journal.
CREATE TABLE IF NOT EXISTS pa_memory_delivery (
    memory_id text PRIMARY KEY,
    revision integer NOT NULL,
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    assistant_id text NOT NULL,
    domain_type text NOT NULL DEFAULT 'assistant' CHECK (domain_type IN ('assistant', 'project', 'personal')),
    domain_id text,
    operation text NOT NULL CHECK (operation IN ('RETAIN', 'DELETE')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (memory_id, revision) REFERENCES pa_memory_revision(memory_id, revision)
);
CREATE INDEX IF NOT EXISTS pa_memory_revision_scope_idx
    ON pa_memory_revision (tenant_id, user_id, assistant_id, memory_id, revision);
CREATE INDEX IF NOT EXISTS pa_memory_delivery_scope_idx
    ON pa_memory_delivery (tenant_id, user_id, assistant_id, created_at, memory_id);

-- Project/personal membership is gateway policy. User/assistant are attribution
-- in shared domains, and remain scope components for legacy assistant records.
CREATE INDEX IF NOT EXISTS pa_memory_domain_idx
    ON pa_memory (tenant_id, domain_type, domain_id, memory_id);
CREATE INDEX IF NOT EXISTS pa_memory_revision_domain_idx
    ON pa_memory_revision (tenant_id, domain_type, domain_id, memory_id, revision);
CREATE INDEX IF NOT EXISTS pa_memory_delivery_domain_idx
    ON pa_memory_delivery (tenant_id, domain_type, domain_id, created_at, memory_id);
