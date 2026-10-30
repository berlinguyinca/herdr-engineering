-- unified workspace P02: the persistent Work Graph (docs/contracts/work-graph-v1.md).
--
-- A READ MODEL over the existing event sources (fabric_events, the artifact registry, the GitHub catalog): every row
-- can be rebuilt from them, nothing here is a second event log.  wg_event is the ledger of source events already
-- projected (idempotence + the reconciler's anti-join); wg_assertion is the immutable evidence linkage (each statement
-- an event made about a subject); wg_entity / wg_edge / wg_fact are the folds of those assertions.  Edges and facts are
-- closed intervals and are never deleted (tombstone = closed interval), so history and as_of reads survive agents
-- exiting.  Layouts are stored apart from graph data.  Additive and idempotent.  Safe to re-run.
--
-- DOWN:
--   DROP TABLE IF EXISTS wg_gap, wg_cursor, wg_layout, wg_fact, wg_edge_interval, wg_edge, wg_alias, wg_entity,
--     wg_assertion, wg_event;

CREATE TABLE IF NOT EXISTS wg_event (
  event_key       text PRIMARY KEY,          -- "<actor>/<event_id>" (event_id is unique per actor) or the registry key
  event_id        text NOT NULL,
  source          text NOT NULL,             -- fabric source, entity type, "registry", "github"
  source_sequence bigint,
  entity_type     text,
  occurred_at     timestamptz,
  trace_id        text,
  assertions      integer NOT NULL DEFAULT 0,
  applied_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_wg_event_source_seq ON wg_event (source, source_sequence);
CREATE INDEX IF NOT EXISTS ix_wg_event_trace ON wg_event (trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS wg_assertion (
  aid             text PRIMARY KEY,          -- "<event_key>#<index>": the same event always yields the same ids
  seq             bigint GENERATED ALWAYS AS IDENTITY,   -- change-feed position only; never part of a comparison
  event_key       text NOT NULL REFERENCES wg_event (event_key),
  source          text NOT NULL,
  source_sequence bigint,
  occurred_at     timestamptz NOT NULL,
  rank            text NOT NULL,             -- total order: (occurred_at, source, source_sequence, event_key, index)
  subject_kind    text NOT NULL CHECK (subject_kind IN ('entity', 'edge', 'fact')),
  subject_id      text NOT NULL,
  mode            text NOT NULL,
  body            jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wg_assertion_subject ON wg_assertion (subject_kind, subject_id, rank);
CREATE INDEX IF NOT EXISTS ix_wg_assertion_event ON wg_assertion (event_key);

CREATE TABLE IF NOT EXISTS wg_entity (
  entity_id       text PRIMARY KEY,          -- urn:herdr:<kind>:<uuid5(kind:native)>
  kind            text NOT NULL,
  native_id       text NOT NULL,             -- HCN id, artifact id, version id, "owner/repo#n", ...
  display_name    text NOT NULL,
  status          text NOT NULL,
  project_id      text,
  visibility      text NOT NULL CHECK (visibility IN ('private', 'mission', 'room', 'org', 'artifact')),
  room_ids        text[] NOT NULL DEFAULT '{}',
  mission_scope   text[] NOT NULL DEFAULT '{}',   -- "<room>|<mission>"
  grantees        text[] NOT NULL DEFAULT '{}',   -- principal ids named by the source ("user:alice")
  attrs           jsonb NOT NULL DEFAULT '{}'::jsonb,
  version         integer NOT NULL DEFAULT 0,
  has_upsert      boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL,
  updated_at      timestamptz NOT NULL,
  tombstoned_at   timestamptz,
  source_event_id text NOT NULL REFERENCES wg_event (event_key),
  UNIQUE (kind, native_id)
);
CREATE INDEX IF NOT EXISTS ix_wg_entity_kind_project ON wg_entity (kind, project_id);
CREATE INDEX IF NOT EXISTS ix_wg_entity_kind_status ON wg_entity (kind, status);
CREATE INDEX IF NOT EXISTS ix_wg_entity_page ON wg_entity (kind, created_at, entity_id);
CREATE INDEX IF NOT EXISTS ix_wg_entity_rooms ON wg_entity USING gin (room_ids);
CREATE INDEX IF NOT EXISTS ix_wg_entity_grantees ON wg_entity USING gin (grantees);
CREATE INDEX IF NOT EXISTS ix_wg_entity_missions ON wg_entity USING gin (mission_scope);
CREATE INDEX IF NOT EXISTS ix_wg_entity_issue ON wg_entity (native_id) WHERE kind = 'issue';
CREATE INDEX IF NOT EXISTS ix_wg_entity_task ON wg_entity (native_id) WHERE kind = 'task';
CREATE INDEX IF NOT EXISTS ix_wg_entity_artifact ON wg_entity (native_id) WHERE kind = 'artifact';
CREATE INDEX IF NOT EXISTS ix_wg_entity_version ON wg_entity ((attrs ->> 'artifact_id')) WHERE kind = 'artifact_version';

CREATE TABLE IF NOT EXISTS wg_alias (
  system          text NOT NULL,             -- collab_task, collab_room, github_issue, pi_session, artifact, ...
  value           text NOT NULL,
  entity_id       text NOT NULL REFERENCES wg_entity (entity_id),
  source_event_id text NOT NULL REFERENCES wg_event (event_key),
  PRIMARY KEY (system, value)                -- an alias is bound once; rebinding to another entity is refused
);
CREATE INDEX IF NOT EXISTS ix_wg_alias_entity ON wg_alias (entity_id);

CREATE TABLE IF NOT EXISTS wg_edge (
  edge_id         text PRIMARY KEY,
  edge_type       text NOT NULL,
  from_id         text NOT NULL REFERENCES wg_entity (entity_id),
  to_id           text NOT NULL REFERENCES wg_entity (entity_id),
  live            boolean NOT NULL,
  valid_from      timestamptz,
  tombstoned_at   timestamptz,               -- close of the last interval (set when not live)
  room_ids        text[] NOT NULL DEFAULT '{}',
  project_id      text,
  source_event_id text NOT NULL REFERENCES wg_event (event_key),
  UNIQUE (edge_type, from_id, to_id)
);
CREATE INDEX IF NOT EXISTS ix_wg_edge_from ON wg_edge (from_id, edge_type) WHERE live;
CREATE INDEX IF NOT EXISTS ix_wg_edge_to ON wg_edge (to_id, edge_type) WHERE live;
CREATE INDEX IF NOT EXISTS ix_wg_edge_from_all ON wg_edge (from_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_wg_edge_to_all ON wg_edge (to_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_wg_edge_project ON wg_edge (project_id);

CREATE TABLE IF NOT EXISTS wg_edge_interval (
  edge_id         text NOT NULL REFERENCES wg_edge (edge_id),
  valid_from      timestamptz NOT NULL,
  valid_to        timestamptz,
  PRIMARY KEY (edge_id, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_wg_edge_interval_range ON wg_edge_interval (valid_from, valid_to);

CREATE TABLE IF NOT EXISTS wg_fact (            -- historical facts: team role assignments, agent sessions, grants
  fact_id         text PRIMARY KEY,
  fact_kind       text NOT NULL,
  subject_ref     text NOT NULL,             -- "user:alice", "agent:a1"
  object_id       text REFERENCES wg_entity (entity_id),
  segments        jsonb NOT NULL,            -- [{from, to|null, attrs, source_event_id}]
  open            boolean NOT NULL,
  room_ids        text[] NOT NULL DEFAULT '{}',
  grantees        text[] NOT NULL DEFAULT '{}',
  project_id      text,
  source_event_id text NOT NULL REFERENCES wg_event (event_key)
);
CREATE INDEX IF NOT EXISTS ix_wg_fact_subject ON wg_fact (subject_ref, fact_kind);
CREATE INDEX IF NOT EXISTS ix_wg_fact_object ON wg_fact (object_id, fact_kind);
CREATE INDEX IF NOT EXISTS ix_wg_fact_rooms ON wg_fact USING gin (room_ids);

CREATE TABLE IF NOT EXISTS wg_layout (          -- diagram layout documents; independent of graph data, survive a rebuild
  scope_id        text NOT NULL,             -- root entity urn the layout is for
  owner_user_id   text NOT NULL DEFAULT '',  -- '' = the shared default; otherwise a per-user override
  document        jsonb NOT NULL,
  version         integer NOT NULL,
  etag            text NOT NULL,
  updated_by      text,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope_id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS wg_cursor (
  name            text PRIMARY KEY,
  watermark_ts    timestamptz,
  watermark_id    text,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wg_gap (             -- what reconciliation found: repaired gaps, source holes, poison events
  gap_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind            text NOT NULL,             -- repaired_event | source_hole | poison_event | retention_gap
  source          text,
  detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
  detected_at     timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz
);
CREATE INDEX IF NOT EXISTS ix_wg_gap_open ON wg_gap (kind) WHERE resolved_at IS NULL;
