-- Per-run resource capture: one row per ended agent session, keyed by project and agent kind (the resource_run ingest
-- event, herdr_engineering/control_plane/resource_runs.py). Numbers, bounded identifiers, UTC times, a closed exit
-- reason and a flag only: never argv, environment, paths or credentials. disk_growth_bytes is NULL when unmeasured.
-- (host, session_id) is the identity, so a replayed event inserts nothing. Additive.  Idempotent.  Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS resource_runs;   -- also drops its index

CREATE TABLE IF NOT EXISTS resource_runs (
  host              text NOT NULL CHECK (host ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,63}$'),
  session_id        text NOT NULL CHECK (session_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  project           text NOT NULL CHECK (char_length(project) BETWEEN 1 AND 128),
  agent_kind        text NOT NULL CHECK (agent_kind ~ '^[a-z][a-z0-9_-]{0,31}$'),
  started_at        timestamptz NOT NULL,
  ended_at          timestamptz NOT NULL,
  peak_memory_bytes bigint NOT NULL CHECK (peak_memory_bytes BETWEEN 0 AND 1125899906842624),
  cpu_seconds       double precision NOT NULL CHECK (cpu_seconds BETWEEN 0 AND 1000000000),
  disk_growth_bytes bigint CHECK (disk_growth_bytes BETWEEN 0 AND 1125899906842624),
  exit_reason       text NOT NULL CHECK (exit_reason IN ('exited', 'completed', 'failed', 'oom', 'disk_full',
                                                         'timeout', 'killed')),
  hit_limit         boolean NOT NULL,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (host, session_id),
  CHECK (ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS resource_runs_profile_idx ON resource_runs (project, agent_kind, ended_at DESC);
