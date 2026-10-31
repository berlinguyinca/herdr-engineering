-- Sign-in sessions of the Rakazo mobile bridge (control_plane.rakazo_bridge). One row per live bearer token, keyed by
-- the SHA-256 of the token: the token itself is shown to the app once and never stored. Rows past expires_at are
-- ignored on load and removed when seen. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS rakazo_bridge_tokens;

CREATE TABLE IF NOT EXISTS rakazo_bridge_tokens (
  token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
  account_id   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  last_used_at timestamptz NOT NULL DEFAULT now(),
  last_ip      text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rakazo_bridge_tokens_account ON rakazo_bridge_tokens (account_id, created_at);
