-- unified workspace P02: a trace id on the event envelope (docs/contracts/unified-workspace-v1.md, event.trace_id).
-- Nullable and additive: v1/v2 rows and writers that do not know the column are unaffected; EventStore names the
-- column in its INSERT only when an event carries one. Safe to re-run.
--
-- DOWN:
--   DROP INDEX IF EXISTS ix_fabric_events_trace_id;
--   ALTER TABLE fabric_events DROP COLUMN IF EXISTS trace_id;

ALTER TABLE fabric_events ADD COLUMN IF NOT EXISTS trace_id text;
CREATE INDEX IF NOT EXISTS ix_fabric_events_trace_id ON fabric_events (trace_id) WHERE trace_id IS NOT NULL;
