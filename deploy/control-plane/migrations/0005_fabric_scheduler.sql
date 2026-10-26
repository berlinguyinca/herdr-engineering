-- Herdr Fabric Scheduler P01-P03 durable state.  Existing host_id remains the
-- display/legacy key; host_uid is the authenticated stable machine identity.
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS host_uid text;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS aliases jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS platform text;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS scheduler_owned boolean NOT NULL DEFAULT false;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS scheduler_trusted boolean NOT NULL DEFAULT false;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS policy_mode text NOT NULL DEFAULT 'manual_only';
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS policy_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS policy_payload jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS idx_hosts_host_uid ON hosts(host_uid) WHERE host_uid IS NOT NULL;

CREATE TABLE IF NOT EXISTS scheduler_reservations (
  reservation_id text PRIMARY KEY,
  request_id text NOT NULL UNIQUE,
  idempotency_key text NOT NULL,
  host_uid text NOT NULL,
  owner_id text NOT NULL,
  project_id text,
  agent_family text NOT NULL,
  workload_class text NOT NULL,
  requested_cpu double precision NOT NULL DEFAULT 0,
  requested_memory_gib double precision NOT NULL DEFAULT 0,
  requested_disk_gib double precision NOT NULL DEFAULT 0,
  state text NOT NULL,
  policy_revision bigint NOT NULL,
  telemetry_sequence bigint,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scheduler_reservations_host_state
  ON scheduler_reservations(host_uid, state, expires_at);

CREATE TABLE IF NOT EXISTS scheduler_decisions (
  decision_id text PRIMARY KEY,
  request_id text NOT NULL,
  selected_host_uid text,
  state text NOT NULL,
  explanation jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scheduler_decisions_request ON scheduler_decisions(request_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scheduler_execution_leases (
  lease_id text PRIMARY KEY,
  request_id text NOT NULL UNIQUE,
  reservation_id text NOT NULL,
  agent_id text,
  host_uid text NOT NULL,
  generation bigint NOT NULL,
  state text NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
