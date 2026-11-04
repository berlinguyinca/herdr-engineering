-- unified workspace P05: durable task leases with fencing tokens (herdr_engineering/control_plane/team_task_leases_pg.py).
--
-- One row per task. ``next_token`` only ever grows, so a successor always holds a higher fencing token than any
-- earlier worker; every state change is one conditional statement, so two control-plane processes cannot both win.
-- The completed result is kept so a retry of the same completion returns it instead of running twice.
-- Additive and idempotent. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS team_task_leases;

CREATE TABLE IF NOT EXISTS team_task_leases (
  task_id     text PRIMARY KEY,
  next_token  bigint NOT NULL DEFAULT 1 CHECK (next_token >= 1),
  token       bigint NOT NULL DEFAULT 0,
  holder      text,
  expires_at  timestamptz,
  revoked     boolean NOT NULL DEFAULT false,
  actions     text[] NOT NULL DEFAULT '{}',
  done        boolean NOT NULL DEFAULT false,
  done_token  bigint,
  result      jsonb,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
