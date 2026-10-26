-- Approval-gated publication records. Provider state is deliberately separate
-- from artifact lifecycle; public delivery always pins an immutable version.

CREATE TABLE publications (
  publication_id       text PRIMARY KEY,
  resource_type        text NOT NULL DEFAULT 'artifact',
  resource_id          text NOT NULL,
  pinned_version_id    text NOT NULL REFERENCES artifact_versions(version_id),
  requested_by         text NOT NULL,
  approved_by          text,
  audience             text NOT NULL,
  token_hash           text NOT NULL UNIQUE,
  provider_id          text NOT NULL,
  desired_state        text NOT NULL DEFAULT 'requested',
  observed_state       text NOT NULL DEFAULT 'unknown',
  endpoint             text,
  expires_at           timestamptz NOT NULL,
  policy_revision      bigint NOT NULL DEFAULT 1,
  provider_error       text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  approved_at          timestamptz,
  revoked_at           timestamptz
);
CREATE INDEX idx_publications_resource ON publications(resource_type, resource_id);
CREATE INDEX idx_publications_state_expiry ON publications(desired_state, expires_at);

CREATE TABLE publication_events (
  event_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  publication_id       text NOT NULL REFERENCES publications(publication_id),
  event_type           text NOT NULL,
  actor_id             text,
  payload              jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_publication_events_publication ON publication_events(publication_id, created_at DESC);
