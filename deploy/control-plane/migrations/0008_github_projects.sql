-- GitHub Projects v2 cache (live-UI/GitHub-cache plan, Task G9). Safe to re-run.
--
-- Read-only Projects v2 boards of the in-scope owners (github.organizations), next to the
-- repository/issue catalog of 0006 and under the same family row: rows carry `rev` from the
-- shared `github` revision, removals are tombstones in github_catalog_tombstones with kind
-- 'project' or 'project_item' and the project id in its repository_id column, and a token or
-- viewer change drops these rows with the rest of the catalog.
-- herdr_engineering/control_plane/github_store.py is the only reader/writer.
--
-- * github_projects: one row per project; `data` holds the listed fields, the Status options and
--   the `synced` probe (updatedAt and item count when its items were last read in full).
-- * github_project_items: one row per item of a project read in full.
--
-- Roll order: the web tolerates these tables being absent (project reads come back empty and
-- project writes fail on their own, without touching the issue catalog), so code and migration
-- can roll in either order.
--
-- DOWN:
--   DROP TABLE IF EXISTS github_project_items, github_projects;
--   DELETE FROM github_catalog_tombstones WHERE kind IN ('project', 'project_item');

CREATE TABLE IF NOT EXISTS github_projects (
  project_id  text PRIMARY KEY,
  owner       text NOT NULL,
  data        jsonb NOT NULL,
  watermark   text,
  rev         bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_github_projects_rev ON github_projects (rev);
CREATE INDEX IF NOT EXISTS ix_github_projects_owner ON github_projects (owner);

CREATE TABLE IF NOT EXISTS github_project_items (
  project_id  text NOT NULL,
  item_id     text NOT NULL,
  data        jsonb NOT NULL,
  rev         bigint NOT NULL,
  PRIMARY KEY (project_id, item_id)
);
CREATE INDEX IF NOT EXISTS ix_github_project_items_rev ON github_project_items (rev);
