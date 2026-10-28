-- Personal Assistant durable store (Rakazo integration).
--
-- Schema for the assistant domain so the control plane can persist assistant
-- state in Postgres (database: herdr_pa). Every table is namespaced by
-- (tenant_id, user_id) for strict isolation; lookups that must not cross
-- tenants/users should always filter both columns. Connector credentials live
-- in the `connectors.auth` jsonb column but MUST never be selected/serialized
-- (the application's as_dict() already omits them). Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS pa_assistants (
    assistant_id  text PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    name          text NOT NULL,
    persona       text NOT NULL DEFAULT 'helpful',
    instructions  text NOT NULL DEFAULT '',
    model_policy  text NOT NULL DEFAULT 'auto',
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_threads (
    thread_id     text PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    assistant_id  text NOT NULL,
    title         text NOT NULL,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_messages (
    message_id    text PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    thread_id     text NOT NULL,
    role          text NOT NULL,
    content       text NOT NULL,
    provenance    text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_runs (
    run_id        text PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    assistant_id  text NOT NULL,
    thread_id     text NOT NULL,
    state         text NOT NULL,
    reply         text NOT NULL DEFAULT '',
    version       integer NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    completed_at  timestamptz
);

CREATE TABLE IF NOT EXISTS pa_actions (
    action_id      text PRIMARY KEY,
    tenant_id      text NOT NULL,
    user_id        text NOT NULL,
    run_id         text NOT NULL,
    tool_id        text NOT NULL,
    scope          text NOT NULL,
    status         text NOT NULL,
    request_hash   text NOT NULL,
    idempotency_key text NOT NULL,
    request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_ref   text NOT NULL DEFAULT '',
    approval_id    text,
    created_at     timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_grants (
    grant_id      text PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    assistant_id  text NOT NULL,
    tool_id       text NOT NULL,
    scope         text NOT NULL,
    constraints   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL,
    expires_at    timestamptz,
    revoked_at    timestamptz
);

CREATE TABLE IF NOT EXISTS pa_approvals (
    approval_id    text PRIMARY KEY,
    tenant_id      text NOT NULL,
    user_id        text NOT NULL,
    assistant_id   text NOT NULL,
    action_id      text NOT NULL,
    actor_id       text NOT NULL DEFAULT '',
    decision       text NOT NULL,
    policy_snapshot text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL,
    expires_at     timestamptz,
    decision_at    timestamptz
);

CREATE TABLE IF NOT EXISTS pa_memory (
    memory_id    text PRIMARY KEY,
    tenant_id    text NOT NULL,
    user_id      text NOT NULL,
    assistant_id text NOT NULL,
    namespace    text NOT NULL DEFAULT 'private',
    content      text NOT NULL,
    source_ref   text NOT NULL DEFAULT '',
    classification text NOT NULL DEFAULT 'general',
    revision     integer NOT NULL DEFAULT 1,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_routines (
    routine_id   text PRIMARY KEY,
    tenant_id    text NOT NULL,
    user_id      text NOT NULL,
    assistant_id text NOT NULL,
    name         text NOT NULL,
    trigger_type text NOT NULL,
    trigger_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    timezone     text NOT NULL DEFAULT 'UTC',
    quiet_hours  jsonb,
    enabled      boolean NOT NULL DEFAULT true,
    last_result  text NOT NULL DEFAULT '',
    last_run_at  timestamptz,
    next_at      timestamptz,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_connectors (
    connector_id text PRIMARY KEY,
    tenant_id    text NOT NULL,
    user_id      text NOT NULL,
    name         text NOT NULL,
    kind         text NOT NULL DEFAULT 'http',
    base_url     text NOT NULL,
    auth         jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled      boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pa_delegations (
    delegation_id text PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    assistant_id  text NOT NULL,
    task          text NOT NULL,
    target_system text NOT NULL DEFAULT 'pi',
    status        text NOT NULL DEFAULT 'queued',
    artifact_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);

-- isolation indexes (always filter tenant_id + user_id)
CREATE INDEX IF NOT EXISTS pa_assistants_tenant_user_idx  ON pa_assistants (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS pa_threads_tenant_user_idx     ON pa_threads (tenant_id, user_id, assistant_id);
CREATE INDEX IF NOT EXISTS pa_messages_thread_idx         ON pa_messages (tenant_id, user_id, thread_id, created_at);
CREATE INDEX IF NOT EXISTS pa_runs_thread_idx             ON pa_runs (tenant_id, user_id, thread_id);
CREATE INDEX IF NOT EXISTS pa_grants_assistant_idx        ON pa_grants (tenant_id, user_id, assistant_id, tool_id);
CREATE INDEX IF NOT EXISTS pa_memory_assistant_idx        ON pa_memory (tenant_id, user_id, assistant_id, namespace);
CREATE INDEX IF NOT EXISTS pa_routines_assistant_idx      ON pa_routines (tenant_id, user_id, assistant_id);
CREATE INDEX IF NOT EXISTS pa_connectors_tenant_user_idx  ON pa_connectors (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS pa_delegations_tenant_user_idx ON pa_delegations (tenant_id, user_id);
