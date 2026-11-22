-- Partitioned counterpart of idx_fabric_events_inspector_sequence (0054). Every index on the live journal needs a `_p`
-- twin on fabric_events_part (0053), or the cutover would leave the partitioned journal without it and the inspector
-- snapshot rebuild would fall back to a seq scan plus sort per page. fabric_events_part is empty until the backfill, so
-- building it here is free. It is skipped when the partitioned table was never prepared (PostgreSQL < 15) or has
-- already been swapped in (it is then named fabric_events and 0054 covered it).
--
-- DOWN:
--   DROP INDEX IF EXISTS idx_fabric_events_inspector_sequence_p;

DO $migration$
BEGIN
  IF to_regclass('fabric_events_part') IS NULL THEN
    RETURN;
  END IF;
  EXECUTE $$CREATE INDEX IF NOT EXISTS idx_fabric_events_inspector_sequence_p
             ON fabric_events_part (sequence) WHERE entity_type = 'inspector'$$;
END
$migration$;
