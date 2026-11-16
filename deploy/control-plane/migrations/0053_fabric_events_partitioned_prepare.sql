-- Design PR 9 (lazy loading): PREPARE a partitioned copy of fabric_events. Nothing here touches the live table's data
-- and nothing here switches traffic: the live table keeps serving every read and write, and its triggers, indexes and
-- constraints are untouched. Only NEW objects are created, so no lock on fabric_events is held beyond a brief
-- ACCESS SHARE while LIKE reads the column list. The copy, the verification and the cutover are an explicit operator
-- step (`herdr-eng control-plane fabric-partition ...`, off unless control_plane.fabric_partitioning.enabled); see
-- docs/operations/fabric-events-partitioning.md.
--
-- Layout: LIST partitioned on entity_type. Inspector rows (the bulk, and the rows a boot replay and autovacuum churn on)
-- get their own heap and indexes; every other entity type, and NULL, lands in the default partition.
-- A unique index on a partitioned table must contain the partition key, so the two uniqueness rules become
-- (actor, event_id, entity_type) and (actor, source, source_sequence, entity_type), NULLS NOT DISTINCT so a NULL
-- entity_type still collides. A retried event keeps its entity_type, so ON CONFLICT DO NOTHING dedup is unchanged; the one
-- rule that weakens is a v2 sequence collision BETWEEN an inspector row and a non-inspector row of the same actor/source.
-- The cutover refuses when any inspector row carries a v2 envelope.
--
-- Index names carry a `_p` suffix until cutover, which renames them to the canonical names (and the old ones to `_old`).
-- Needs PostgreSQL 15+ (NULLS NOT DISTINCT); on older servers this migration only records a NOTICE and creates nothing.
--
-- DOWN (before any cutover; harmless to run, drops only the prepared copy):
--   DROP TABLE IF EXISTS fabric_events_part;   -- cascades to its partitions and indexes
--   DROP TABLE IF EXISTS fabric_events_partition_state;
--   DROP FUNCTION IF EXISTS fabric_events_part_assign_position(), fabric_events_part_record_change(),
--                           fabric_events_part_record_truncate();

CREATE TABLE IF NOT EXISTS fabric_events_partition_state (
  singleton        boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  phase            text NOT NULL DEFAULT 'prepared'
                   CHECK (phase IN ('prepared', 'backfilling', 'backfilled', 'partitioned')),
  start_position   bigint,            -- projection_position high-water mark when the backfill started
  cursor_actor     text,              -- keyset cursor of the backfill: (COALESCE(actor->>'id',''), event_id)
  cursor_event     text,
  copied_rows      bigint NOT NULL DEFAULT 0,
  cutover_position bigint,            -- high-water mark when traffic moved to the partitioned table
  updated_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO fabric_events_partition_state (singleton) VALUES (true) ON CONFLICT DO NOTHING;

DO $migration$
BEGIN
  IF current_setting('server_version_num')::int < 150000 THEN
    RAISE NOTICE 'fabric_events partitioning needs PostgreSQL 15+; nothing prepared';
    RETURN;
  END IF;
  IF to_regclass('fabric_events_part') IS NOT NULL THEN
    RETURN;
  END IF;

  -- Same columns and defaults as the live journal (so later column additions are visible to the preflight check).
  EXECUTE 'CREATE TABLE fabric_events_part (LIKE fabric_events INCLUDING DEFAULTS) PARTITION BY LIST (entity_type)';
  EXECUTE $$CREATE TABLE fabric_events_part_inspector PARTITION OF fabric_events_part FOR VALUES IN ('inspector')$$;
  EXECUTE 'CREATE TABLE fabric_events_part_other PARTITION OF fabric_events_part DEFAULT';

  EXECUTE 'CREATE UNIQUE INDEX uq_fabric_events_actor_event_id_p
             ON fabric_events_part ((COALESCE(actor ->> ''id'', '''')), event_id, entity_type) NULLS NOT DISTINCT';
  EXECUTE 'CREATE UNIQUE INDEX uq_fabric_events_actor_source_sequence_p
             ON fabric_events_part ((COALESCE(actor ->> ''id'', '''')), source, source_sequence, entity_type) NULLS NOT DISTINCT
             WHERE source IS NOT NULL AND source_sequence IS NOT NULL';
  EXECUTE 'CREATE INDEX idx_fabric_events_stream_p ON fabric_events_part (entity_type, entity_id, sequence)';
  EXECUTE 'CREATE INDEX idx_fabric_events_type_source_ts_p ON fabric_events_part (entity_type, source_timestamp)';
  EXECUTE 'CREATE INDEX ix_fabric_events_trace_id_p ON fabric_events_part (trace_id) WHERE trace_id IS NOT NULL';
  EXECUTE 'CREATE INDEX idx_fabric_events_recovery_arrival_p
             ON fabric_events_part (entity_type, entity_id, ingest_timestamp, sequence, event_id)';
  EXECUTE 'CREATE INDEX idx_fabric_events_recovery_instance_p
             ON fabric_events_part ((COALESCE(actor ->> ''id'', '''')), event_id) INCLUDE (actor, recovery_instance)';
  EXECUTE 'CREATE INDEX idx_fabric_events_recovery_identity_p
             ON fabric_events_part ((COALESCE(actor ->> ''id'', '''')), event_id) INCLUDE (actor)';
  -- The projection position index normally comes from `control-plane checkpoint` (CONCURRENTLY, which a partitioned
  -- table cannot do); here the table is empty, so building it is free.
  EXECUTE 'CREATE INDEX idx_fabric_events_projection_position_p
             ON fabric_events_part (projection_position) WHERE projection_position IS NOT NULL';
END
$migration$;

-- Trigger functions for the partitioned table. The live ones derive the writer-fence key from TG_TABLE_NAME, which on a
-- partitioned table is the PARTITION's name; the materializer takes the fence on schema.fabric_events, so these copies
-- pin the key to the parent. They are attached to the new table only at cutover (never during the backfill, which must
-- preserve projection_position and recovery_instance and log nothing).
CREATE OR REPLACE FUNCTION fabric_events_part_assign_position() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(1213355076, hashtext(TG_TABLE_SCHEMA || '.fabric_events'));
  NEW.projection_position := nextval(format('%I.projection_position_seq',TG_TABLE_SCHEMA)::regclass);
  RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION fabric_events_part_record_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  position_value bigint;
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM pg_advisory_xact_lock_shared(1213355076, hashtext(TG_TABLE_SCHEMA || '.fabric_events'));
    position_value := nextval(format('%I.projection_position_seq',TG_TABLE_SCHEMA)::regclass);
    EXECUTE format('INSERT INTO %I.projection_event_changes(position,operation,entity_type,entity_id,event_id,actor_id,had_position,has_envelope) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',TG_TABLE_SCHEMA)
      USING position_value,'D',OLD.entity_type,OLD.entity_id,OLD.event_id,
            COALESCE(OLD.actor->>'id',''),OLD.projection_position,OLD.source_sequence IS NOT NULL;
    IF OLD.projection_position IS NULL THEN
      EXECUTE format('UPDATE %I.projection_checkpoint_control SET legacy_rows=legacy_rows-1 WHERE singleton',TG_TABLE_SCHEMA);
    END IF;
    RETURN OLD;
  END IF;
  EXECUTE format('INSERT INTO %I.projection_event_changes(position,operation,entity_type,entity_id,event_id,actor_id,had_position,has_envelope) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',TG_TABLE_SCHEMA)
    USING NEW.projection_position,CASE WHEN TG_OP='INSERT' THEN 'I' ELSE 'U' END,
          NEW.entity_type,NEW.entity_id,NEW.event_id,COALESCE(NEW.actor->>'id',''),
          CASE WHEN TG_OP='UPDATE' THEN OLD.projection_position ELSE NULL END,
          NEW.source_sequence IS NOT NULL;
  IF TG_OP='UPDATE' AND OLD.projection_position IS NULL THEN
    EXECUTE format('UPDATE %I.projection_checkpoint_control SET legacy_rows=legacy_rows-1 WHERE singleton',TG_TABLE_SCHEMA);
  END IF;
  RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION fabric_events_part_record_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(1213355076, hashtext(TG_TABLE_SCHEMA || '.fabric_events'));
  EXECUTE format('INSERT INTO %I.projection_event_changes(position,operation) VALUES(nextval(%L::regclass),%L)',
                 TG_TABLE_SCHEMA,format('%I.projection_position_seq',TG_TABLE_SCHEMA),'T');
  EXECUTE format('UPDATE %I.projection_checkpoint_control SET legacy_rows=0 WHERE singleton',TG_TABLE_SCHEMA);
  RETURN NULL;
END;
$$;
