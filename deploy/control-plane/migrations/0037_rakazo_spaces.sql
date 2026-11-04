-- Spaces of the Rakazo mobile bridge (control_plane.rakazo_bridge). A space is a derived assistant tenant, so this table holds
-- only the registry: which account owns which extra space, its name, the settings it customises, and a tombstone. The
-- account's default space has no row. Rows are never deleted (a removed space keeps deleted = true so its id is not reused).
-- Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS rakazo_spaces;

CREATE TABLE IF NOT EXISTS rakazo_spaces (
  space_id   text PRIMARY KEY CHECK (space_id ~ '^spc[0-9a-f]{20}$'),
  account_id text NOT NULL,
  name       text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
  overrides  text[] NOT NULL DEFAULT '{}',
  deleted    boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rakazo_spaces_account ON rakazo_spaces (account_id, created_at);
