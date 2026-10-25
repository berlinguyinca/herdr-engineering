-- GitHub issue detail cache (live-UI/GitHub-cache plan, Task G6). Safe to re-run.
--
-- Issue bodies are stored only for issues whose detail a viewer opened, together with the issue's
-- `updated_at` at fetch time. The web serves the stored body while the catalog row's `updatedAt`
-- is unchanged and refetches once the incremental pass records a newer one (no TTL).
-- herdr_engineering/control_plane/github_store.py is the only reader/writer. Rows are removed with
-- their issue or repository and on a token/viewer change, like the catalog in 0006; they never
-- change the catalog revision (bodies are not part of the listed catalog).
--
-- Roll order: the web tolerates this table being absent (detail stays in memory), so code and
-- migration can roll in either order.
--
-- DOWN:
--   DROP TABLE IF EXISTS github_issue_details;

CREATE TABLE IF NOT EXISTS github_issue_details (
  repository_id  text NOT NULL,
  github_id      text NOT NULL,
  number         integer NOT NULL,
  updated_at     text,
  data           jsonb NOT NULL,
  fetched_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repository_id, github_id)
);
CREATE INDEX IF NOT EXISTS ix_github_issue_details_number ON github_issue_details (repository_id, number);
