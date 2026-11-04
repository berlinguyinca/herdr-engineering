-- Expo push tokens the Rakazo phone app registers with the bridge (control_plane.rakazo_bridge.push). At most a few
-- per account. A token only lets this server ask Expo to notify that one device; it is not a sign-in credential.
-- Rows are removed when Expo reports the device gone or the app unregisters. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS rakazo_push_tokens;

CREATE TABLE IF NOT EXISTS rakazo_push_tokens (
  account_id     text NOT NULL,
  token          text NOT NULL CHECK (char_length(token) BETWEEN 8 AND 512),
  session_sha256 text NOT NULL DEFAULT '',
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, token)
);
