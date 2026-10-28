-- Versioned artifact registry (spec 2026-10-25, work package 1).
-- Existing artifacts/bindings/materializations remain compatible.  Large
-- payloads and manifests are stored in the existing S3-compatible backend;
-- PostgreSQL stores identity, authorization, provenance and state only.

CREATE TABLE artifact_blobs (
  blob_sha256     text NOT NULL,
  scope           text NOT NULL DEFAULT 'fabric',
  storage_backend text NOT NULL DEFAULT 'rustfs',
  storage_key     text NOT NULL,
  size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
  media_type      text,
  verified_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope, blob_sha256),
  UNIQUE (scope, storage_key)
);
CREATE INDEX idx_artifact_blobs_sha256 ON artifact_blobs(blob_sha256);

CREATE TABLE artifact_versions (
  version_id             text PRIMARY KEY,
  artifact_id            text NOT NULL REFERENCES artifacts(artifact_id),
  version_number         int NOT NULL CHECK (version_number > 0),
  parent_version_id      text REFERENCES artifact_versions(version_id),
  base_version_id        text REFERENCES artifact_versions(version_id),
  state                  text NOT NULL DEFAULT 'draft',
  source_manifest_blob   text,
  output_manifest        jsonb NOT NULL DEFAULT '[]'::jsonb,
  provenance             jsonb NOT NULL DEFAULT '{}'::jsonb,
  validation             jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by             text,
  agent_id               text REFERENCES agents(agent_id),
  host_id                text REFERENCES hosts(host_id),
  session_id             text REFERENCES sessions(session_id),
  mission_id             text REFERENCES missions(mission_id),
  job_id                 text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  committed_at           timestamptz,
  UNIQUE (artifact_id, version_number),
  UNIQUE (version_id, artifact_id)
);
CREATE INDEX idx_artifact_versions_artifact
  ON artifact_versions(artifact_id, version_number DESC);
CREATE INDEX idx_artifact_versions_state
  ON artifact_versions(state, created_at DESC);
CREATE INDEX idx_artifact_versions_provenance
  ON artifact_versions(host_id, session_id, mission_id);

ALTER TABLE artifacts
  ADD COLUMN current_version_id text,
  ADD COLUMN workspace_id text,
  ADD COLUMN lifecycle text NOT NULL DEFAULT 'active';
ALTER TABLE artifacts
  ADD CONSTRAINT fk_artifacts_current_version
  FOREIGN KEY (current_version_id) REFERENCES artifact_versions(version_id)
  DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX idx_artifacts_workspace ON artifacts(workspace_id);
CREATE INDEX idx_artifacts_lifecycle ON artifacts(lifecycle, created_at DESC);

CREATE TABLE artifact_idempotency (
  principal_id   text NOT NULL,
  idempotency_key text NOT NULL,
  request_sha256 text NOT NULL,
  artifact_id    text REFERENCES artifacts(artifact_id),
  version_id     text REFERENCES artifact_versions(version_id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, idempotency_key)
);

CREATE TABLE artifact_reviews (
  review_id       text PRIMARY KEY,
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  version_id      text NOT NULL REFERENCES artifact_versions(version_id),
  requested_by    text,
  reviewer        text,
  state           text NOT NULL DEFAULT 'requested',
  findings        jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz
);
CREATE INDEX idx_artifact_reviews_version ON artifact_reviews(version_id, state);

CREATE TABLE artifact_comments (
  comment_id      text PRIMARY KEY,
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  version_id      text NOT NULL REFERENCES artifact_versions(version_id),
  author_id       text NOT NULL,
  body            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_artifact_comments_version ON artifact_comments(version_id, created_at);

CREATE TABLE artifact_dependencies (
  artifact_id       text NOT NULL REFERENCES artifacts(artifact_id),
  version_id        text NOT NULL REFERENCES artifact_versions(version_id),
  dependency_kind   text NOT NULL,
  dependency_id     text NOT NULL,
  dependency_revision text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, dependency_kind, dependency_id)
);

CREATE TABLE artifact_build_jobs (
  job_id             text PRIMARY KEY,
  artifact_id        text REFERENCES artifacts(artifact_id),
  base_version_id    text REFERENCES artifact_versions(version_id),
  profile            text NOT NULL,
  request            jsonb NOT NULL DEFAULT '{}'::jsonb,
  status             text NOT NULL DEFAULT 'queued',
  requested_by       text,
  assigned_host_id   text REFERENCES hosts(host_id),
  scheduler_task_id  text,
  result_version_id  text REFERENCES artifact_versions(version_id),
  error              jsonb,
  created_at         timestamptz NOT NULL DEFAULT now(),
  started_at         timestamptz,
  completed_at       timestamptz
);
CREATE INDEX idx_artifact_build_jobs_status ON artifact_build_jobs(status, created_at);
CREATE INDEX idx_artifact_build_jobs_host ON artifact_build_jobs(assigned_host_id, status);

CREATE TABLE artifact_audit (
  audit_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  artifact_id     text REFERENCES artifacts(artifact_id),
  version_id      text REFERENCES artifact_versions(version_id),
  action          text NOT NULL,
  principal_id    text,
  host_id         text REFERENCES hosts(host_id),
  session_id      text REFERENCES sessions(session_id),
  request_id      text,
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_artifact_audit_artifact ON artifact_audit(artifact_id, created_at DESC);
CREATE INDEX idx_artifact_audit_version ON artifact_audit(version_id, created_at DESC);

CREATE TABLE artifact_collections (
  collection_id   text PRIMARY KEY,
  workspace_id    text,
  name            text NOT NULL,
  created_by      text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE artifact_collection_revisions (
  revision_id     text PRIMARY KEY,
  collection_id   text NOT NULL REFERENCES artifact_collections(collection_id),
  revision_number int NOT NULL CHECK (revision_number > 0),
  members         jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_by      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (collection_id, revision_number)
);
