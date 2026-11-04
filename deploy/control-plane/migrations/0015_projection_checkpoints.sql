-- Complete projection recovery. Legacy rows stay NULL: no historical rewrite.
-- Allocate positions only after taking the writer fence. A sequence DEFAULT
-- would allow a lower allocated ID to commit after a checkpoint frontier.
CREATE SEQUENCE projection_position_seq CACHE 1;
ALTER TABLE fabric_events ADD COLUMN projection_position bigint;
CREATE INDEX idx_fabric_events_projection_position
  ON fabric_events(projection_position) WHERE projection_position IS NOT NULL;

CREATE TABLE projection_event_changes (
  position bigint PRIMARY KEY,
  operation text NOT NULL CHECK (operation IN ('I','D','U','T')),
  entity_type text,
  entity_id text,
  event_id text,
  actor_id text NOT NULL DEFAULT '',
  had_position bigint,
  has_envelope boolean NOT NULL DEFAULT false
);
CREATE TABLE projection_checkpoints (
  checkpoint_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code_fingerprint text NOT NULL,
  policy_fingerprint text NOT NULL,
  frontier bigint NOT NULL,
  format_version integer NOT NULL,
  state bytea NOT NULL,
  checksum text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(code_fingerprint,policy_fingerprint,frontier)
);
CREATE TABLE projection_checkpoint_control (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  replay_floor bigint NOT NULL DEFAULT 0,
  legacy_rows bigint NOT NULL DEFAULT 0
);
INSERT INTO projection_checkpoint_control(singleton,legacy_rows)
  SELECT true,count(*) FROM fabric_events WHERE projection_position IS NULL;

CREATE FUNCTION projection_assign_position() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(1213355076, hashtext(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME));
  NEW.projection_position := nextval(format('%I.projection_position_seq',TG_TABLE_SCHEMA)::regclass);
  RETURN NEW;
END;
$$;
CREATE FUNCTION projection_record_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  position_value bigint;
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM pg_advisory_xact_lock_shared(1213355076, hashtext(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME));
    position_value := nextval(format('%I.projection_position_seq',TG_TABLE_SCHEMA)::regclass);
    EXECUTE format('INSERT INTO %I.projection_event_changes(position,operation,entity_type,entity_id,event_id,actor_id,had_position,has_envelope) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',TG_TABLE_SCHEMA)
      USING position_value,'D',OLD.entity_type,OLD.entity_id,OLD.event_id,
            COALESCE(OLD.actor->>'id',''),OLD.projection_position,OLD.source_sequence IS NOT NULL;
    IF OLD.projection_position IS NULL THEN
      EXECUTE format('UPDATE %I.projection_checkpoint_control SET legacy_rows=legacy_rows-1 WHERE singleton',TG_TABLE_SCHEMA);
    END IF;
    RETURN OLD;
  END IF;
  -- AFTER INSERT excludes ON CONFLICT DO NOTHING attempts from the change log.
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
CREATE FUNCTION projection_record_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock_shared(1213355076, hashtext(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME));
  EXECUTE format('INSERT INTO %I.projection_event_changes(position,operation) VALUES(nextval(%L::regclass),%L)',
                 TG_TABLE_SCHEMA,format('%I.projection_position_seq',TG_TABLE_SCHEMA),'T');
  EXECUTE format('UPDATE %I.projection_checkpoint_control SET legacy_rows=0 WHERE singleton',TG_TABLE_SCHEMA);
  RETURN NULL;
END;
$$;
CREATE TRIGGER projection_position_before_write BEFORE INSERT OR UPDATE ON fabric_events
  FOR EACH ROW EXECUTE FUNCTION projection_assign_position();
CREATE TRIGGER projection_change_after_write AFTER INSERT OR UPDATE OR DELETE ON fabric_events
  FOR EACH ROW EXECUTE FUNCTION projection_record_change();
CREATE TRIGGER projection_change_after_truncate AFTER TRUNCATE ON fabric_events
  FOR EACH STATEMENT EXECUTE FUNCTION projection_record_truncate();
