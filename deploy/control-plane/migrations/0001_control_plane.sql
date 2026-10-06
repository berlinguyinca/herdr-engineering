-- Phase-1 control-plane schema (Spec §53). Identity is stable; location is dynamic.

CREATE TABLE hosts (
  host_id         text PRIMARY KEY,
  tailnet_ip      text,
  hostname        text,
  status          text NOT NULL DEFAULT 'unknown',   -- healthy|degraded|unavailable|unknown
  reason          text,
  detail          text,
  last_heartbeat  timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE host_samples (
  sample_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  host_id         text NOT NULL REFERENCES hosts(host_id),
  sampled_at      timestamptz NOT NULL DEFAULT now(),
  cpu_percent     double precision,
  load_avg        double precision,
  mem_used_bytes  bigint,
  mem_total_bytes bigint,
  disk_free_bytes bigint,
  disk_total_bytes bigint,
  gpu_util_percent double precision,
  vram_used_bytes bigint,
  payload         jsonb
);
CREATE INDEX idx_host_samples_host_time ON host_samples(host_id, sampled_at DESC);

CREATE TABLE missions (
  mission_id      text PRIMARY KEY,
  title           text NOT NULL,
  purpose         text,
  repository      text,
  branch          text,
  worktree        text,
  owner           text,
  stage           text NOT NULL DEFAULT 'planning',
  status          text NOT NULL DEFAULT 'active',
  progress        double precision NOT NULL DEFAULT 0,
  current_activity jsonb,
  blocked         boolean NOT NULL DEFAULT false,
  blocking_reason text,
  blocking_detail text,
  blocked_since   timestamptz,
  active_session_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  outcome         text
);
CREATE INDEX idx_missions_status ON missions(status);
CREATE INDEX idx_missions_repository ON missions(repository);

CREATE TABLE mission_events (
  event_id        text PRIMARY KEY,
  mission_id      text NOT NULL REFERENCES missions(mission_id),
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  UNIQUE (mission_id, sequence)
);
CREATE INDEX idx_mission_events_type ON mission_events(event_type);

CREATE TABLE mission_stage_runs (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text NOT NULL REFERENCES missions(mission_id),
  stage           text NOT NULL,
  attempt         int NOT NULL DEFAULT 1,
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  duration_ms     bigint,
  status          text,
  session_id      text,
  model           text
);

CREATE TABLE plans (
  plan_id         text PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  planator_plan_id text,
  version         int NOT NULL DEFAULT 1,
  status          text NOT NULL DEFAULT 'draft',
  payload         jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  approved_at     timestamptz
);
CREATE TABLE plan_revisions (
  revision_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id         text NOT NULL REFERENCES plans(plan_id),
  version         int NOT NULL,
  payload         jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
  session_id      text PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  title           text,
  purpose         text,
  agent_role      text,          -- planner|implementer|tester|reviewer|repair
  model           text,
  host_id         text REFERENCES hosts(host_id),
  status          text NOT NULL DEFAULT 'active',
  stage           text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  last_activity   timestamptz,
  ended_at        timestamptz,
  payload         jsonb
);
CREATE INDEX idx_sessions_mission ON sessions(mission_id);
CREATE INDEX idx_sessions_host ON sessions(host_id);
CREATE INDEX idx_sessions_status ON sessions(status);

CREATE TABLE session_events (
  event_id        text PRIMARY KEY,
  session_id      text NOT NULL REFERENCES sessions(session_id),
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, sequence)
);

CREATE TABLE agents (
  agent_id        text PRIMARY KEY,
  session_id      text REFERENCES sessions(session_id),
  model           text,
  role            text,
  status          text NOT NULL DEFAULT 'active',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent_runs (
  run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_id        text NOT NULL REFERENCES agents(agent_id),
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  status          text,
  model           text,
  payload         jsonb
);

CREATE TABLE services (
  service_id      text PRIMARY KEY,
  service_name    text NOT NULL,
  route           text,                 -- e.g. http://dev.lan:18000
  backend         text,
  port            int,
  host_id         text REFERENCES hosts(host_id),
  owner           text,
  started_by      text,
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  workspace_id    text,
  repository      text,
  worktree        text,
  branch          text,
  purpose         text,
  health          text NOT NULL DEFAULT 'unknown',
  started_at      timestamptz NOT NULL DEFAULT now(),
  last_seen       timestamptz,
  stopped_at      timestamptz,
  runtime         jsonb             -- pid/container_id/systemd_unit/slurm_job_id (metadata only)
);
CREATE INDEX idx_services_host ON services(host_id);
CREATE INDEX idx_services_mission ON services(mission_id);
CREATE INDEX idx_services_session ON services(session_id);
CREATE INDEX idx_services_repository ON services(repository);
CREATE TABLE service_events (
  event_id        text PRIMARY KEY,
  service_id      text NOT NULL REFERENCES services(service_id),
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  UNIQUE (service_id, sequence)
);

CREATE TABLE artifacts (
  artifact_id     text PRIMARY KEY,
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text,
  original_filename text,
  display_name    text,
  mime_type       text,
  artifact_type   text NOT NULL DEFAULT 'other',
  source          text,
  size_bytes      bigint,
  sha256          text,
  storage_backend text NOT NULL DEFAULT 'rustfs',
  storage_key     text,
  status          text NOT NULL DEFAULT 'uploading', -- uploading|verifying|available|failed
  retention_class text NOT NULL DEFAULT 'temporary', -- temporary|session|mission|permanent
  payload         jsonb
);
CREATE INDEX idx_artifacts_sha256 ON artifacts(sha256);

CREATE TABLE artifact_bindings (
  binding_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  agent_id        text REFERENCES agents(agent_id),
  stage           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text,
  UNIQUE (artifact_id, mission_id, COALESCE(session_id, ''), COALESCE(stage, ''))
);

CREATE TABLE artifact_materializations (
  materialization_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  host_id         text REFERENCES hosts(host_id),
  session_id      text REFERENCES sessions(session_id),
  local_path      text,
  status          text NOT NULL DEFAULT 'pending',
  sha256_verified boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE token_usage (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  agent_id        text REFERENCES agents(agent_id),
  model           text,
  provider        text,
  stage           text,
  request_id      text,
  input_tokens    bigint,
  output_tokens   bigint,
  reasoning_tokens bigint,
  cache_read_tokens bigint,
  cache_write_tokens bigint,
  duration_ms     bigint,
  host_id         text REFERENCES hosts(host_id),
  retry_number    int,
  success         boolean,
  error_code      text
);
CREATE INDEX idx_token_usage_mission ON token_usage(mission_id, occurred_at);
CREATE INDEX idx_token_usage_model ON token_usage(model);

CREATE TABLE resource_usage (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  kind            text,
  value           double precision,
  unit            text,
  payload         jsonb
);

CREATE TABLE test_runs (
  test_run_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  repository      text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  status          text,
  passed          int,
  failed          int,
  skipped         int,
  total           int
);
CREATE TABLE test_results (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  test_run_id     bigint NOT NULL REFERENCES test_runs(test_run_id),
  name            text,
  status          text,
  duration_ms     bigint,
  message         text
);

CREATE TABLE reviews (
  review_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  reviewer        text,
  status          text,
  verdict         text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz
);
CREATE TABLE review_findings (
  finding_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  review_id       bigint NOT NULL REFERENCES reviews(review_id),
  severity        text,
  summary         text,
  detail          text,
  status          text
);

CREATE TABLE pull_requests (
  pr_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  repository      text,
  number          int,
  url             text,
  status          text,
  branch          text,
  base            text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
  event_id        text PRIMARY KEY,
  actor           text,
  action          text NOT NULL,
  entity_type     text,
  entity_id       text,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  metadata        jsonb
);

CREATE TABLE fabric_events (
  event_id        text PRIMARY KEY,
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  entity_type     text,
  entity_id       text,
  mission_id      text,
  session_id      text,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  schema_version  int NOT NULL DEFAULT 1
);
CREATE INDEX idx_fabric_events_stream ON fabric_events(entity_type, entity_id, sequence);

CREATE TABLE state_snapshots (
  snapshot_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_kind     text NOT NULL,       -- mission|session|host|service
  entity_id       text NOT NULL,
  sequence        bigint NOT NULL DEFAULT 0,
  state           jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_kind, entity_id, sequence)
);
