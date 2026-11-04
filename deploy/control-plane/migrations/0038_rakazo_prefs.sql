-- Small per-account, per-space settings of the Rakazo mobile bridge (control_plane.rakazo_bridge): pinned or archived groups,
-- read markers, trusted link sites, and similar. One row per (account, space, kind, key); value is JSON. Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS rakazo_prefs;

CREATE TABLE IF NOT EXISTS rakazo_prefs (
  account_id text NOT NULL,
  space_id   text NOT NULL,
  kind       text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 40),
  key        text NOT NULL CHECK (char_length(key) BETWEEN 1 AND 200),
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, space_id, kind, key)
);
