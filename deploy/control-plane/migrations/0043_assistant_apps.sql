-- Per-person app connections for the assistant (Gmail, Calendar). Tokens and client secrets are stored only as Fernet
-- ciphertext (the key is a file on the control plane, never in the database). Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS assistant_app_blocked;
--   DROP TABLE IF EXISTS assistant_app_state;   -- also drops its expires_at index
--   DROP TABLE IF EXISTS assistant_app_connections;
--   DROP TABLE IF EXISTS assistant_app_clients;

CREATE TABLE IF NOT EXISTS assistant_app_clients (
  provider   text PRIMARY KEY,
  client_id  text NOT NULL,
  secret_ct  text NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_app_connections (
  tenant_id  text NOT NULL,
  user_id    text NOT NULL,
  provider   text NOT NULL,
  account    text NOT NULL DEFAULT '',
  scopes     jsonb NOT NULL DEFAULT '[]',
  status     text NOT NULL DEFAULT 'connected' CHECK (status IN ('connected', 'needs_attention')),
  token_ct   text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id, provider)
);

-- One row per in-flight OAuth consent: only the hash of the state value is kept, plus the sealed PKCE verifier.
CREATE TABLE IF NOT EXISTS assistant_app_state (
  state_sha256 text PRIMARY KEY,
  tenant_id    text NOT NULL,
  user_id      text NOT NULL,
  provider     text NOT NULL,
  verifier_ct  text NOT NULL,
  expires_at   timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS assistant_app_state_expires_idx ON assistant_app_state (expires_at);

-- Calendars a person has blocked from the assistant. Separate from the connection so it survives disconnect and
-- reconnect. Calendar ids are not secrets.
CREATE TABLE IF NOT EXISTS assistant_app_blocked (
  tenant_id    text NOT NULL,
  user_id      text NOT NULL,
  provider     text NOT NULL,
  calendar_ids jsonb NOT NULL DEFAULT '[]',
  PRIMARY KEY (tenant_id, user_id, provider)
);
