-- Event envelope v2 (docs/contracts/event-envelope-v2.md, T01): extend the
-- fabric_events log with the canonical envelope fields. Every column is
-- nullable so schema_version 1 rows and writers keep working unchanged.

ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS source           text;    -- sequence domain
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS source_sequence  bigint;  -- strictly increasing per source
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS causation_id     text;    -- event_id of the causing event
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS correlation_id   text;    -- mission/flow grouping
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS actor            jsonb;   -- {kind,id,authenticated_by}; server-set
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS visibility_scope text;    -- private|mission|room|org
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS evidence_refs    jsonb;   -- [{kind,uri,digest?,revision?}]
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS org_id           text;
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS project_id       text;
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS room_id          text;
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS workspace_id     text;
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS task_id          text;
ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS agent_id         text;

-- A (source, source_sequence) pair identifies exactly one event.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fabric_events_source_sequence
  ON fabric_events(source, source_sequence)
  WHERE source IS NOT NULL AND source_sequence IS NOT NULL;

-- Per-source ingest state: last sequence, freshness, and open gaps.
CREATE TABLE IF NOT EXISTS fabric_event_sources (
  source        text PRIMARY KEY,
  last_sequence bigint,
  last_seen_at  timestamptz,
  gaps          jsonb NOT NULL DEFAULT '[]'::jsonb  -- [{from,to,seen_at}]
);
