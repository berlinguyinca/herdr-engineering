-- GitHub catalog cache (live-UI/GitHub-cache plan, Task G1). Safe to re-run.
--
-- The web control plane persists the GitHub repository/issue catalog here so a restart serves
-- the stored catalog instead of re-reading GitHub. herdr_engineering/control_plane/github_store.py
-- is the only reader/writer.
--
-- * github_catalog_meta: one row per resource family ('github'). `identity` is the SHA-256
--   fingerprint of the configured token (never the token) and `viewer` the GitHub login; on a
--   mismatch the store drops every row below. `revision` increases on every visible change and
--   is never reset; `pruned_through` is the newest revision whose tombstone has been discarded
--   (a client `since` below it must resync).
-- * rows carry `rev`, the family revision of their last change; payloads are jsonb so later
--   fields (probe values, detail) need no new migration.
-- * github_catalog_tombstones keep removals for 7 days (pruned by the store).
--
-- Roll order: the web tolerates these tables being absent (the store reports unavailable and
-- the monitor stays in memory), so code and migration can roll in either order.
--
-- DOWN:
--   DROP TABLE IF EXISTS github_catalog_tombstones, github_issues, github_repositories,
--     github_catalog_meta;

CREATE TABLE IF NOT EXISTS github_catalog_meta (
  family          text PRIMARY KEY,
  identity        text NOT NULL,
  viewer          text,
  revision        bigint NOT NULL DEFAULT 0,
  pruned_through  bigint NOT NULL DEFAULT 0,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS github_repositories (
  repository_id  text PRIMARY KEY,
  owner          text NOT NULL,
  data           jsonb NOT NULL,
  watermark      text,
  rev            bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_github_repositories_rev ON github_repositories (rev);
CREATE INDEX IF NOT EXISTS ix_github_repositories_owner ON github_repositories (owner);

CREATE TABLE IF NOT EXISTS github_issues (
  repository_id  text NOT NULL,
  github_id      text NOT NULL,
  data           jsonb NOT NULL,
  rev            bigint NOT NULL,
  PRIMARY KEY (repository_id, github_id)
);
CREATE INDEX IF NOT EXISTS ix_github_issues_rev ON github_issues (rev);

CREATE TABLE IF NOT EXISTS github_catalog_tombstones (
  kind           text NOT NULL,
  repository_id  text NOT NULL,
  key            text NOT NULL,
  rev            bigint NOT NULL,
  removed_at     timestamptz NOT NULL,
  PRIMARY KEY (kind, repository_id, key)
);
CREATE INDEX IF NOT EXISTS ix_github_catalog_tombstones_rev ON github_catalog_tombstones (rev);
CREATE INDEX IF NOT EXISTS ix_github_catalog_tombstones_removed_at
  ON github_catalog_tombstones (removed_at);
