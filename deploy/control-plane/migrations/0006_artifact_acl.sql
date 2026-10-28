-- Artifact-scoped authorization. No global hash/ID implies access.

CREATE TABLE artifact_acl (
  artifact_id   text NOT NULL REFERENCES artifacts(artifact_id),
  principal_id  text NOT NULL,
  permissions   jsonb NOT NULL DEFAULT '["read"]'::jsonb,
  expires_at    timestamptz,
  granted_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (artifact_id, principal_id)
);
CREATE INDEX idx_artifact_acl_principal ON artifact_acl(principal_id, expires_at);
