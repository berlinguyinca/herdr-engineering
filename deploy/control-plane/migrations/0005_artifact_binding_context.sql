-- Extend existing artifact bindings with explicit contextual relationships.
-- The existing artifact_id/mission/session/agent columns remain authoritative;
-- these nullable fields add room/workspace/issue/commit context without guessed
-- joins or a second attachment table.

ALTER TABLE artifact_bindings
  ADD COLUMN workspace_id text,
  ADD COLUMN room_id text,
  ADD COLUMN issue_ref text,
  ADD COLUMN commit_sha text;
CREATE INDEX idx_artifact_bindings_workspace ON artifact_bindings(workspace_id);
CREATE INDEX idx_artifact_bindings_room ON artifact_bindings(room_id);
CREATE INDEX idx_artifact_bindings_issue ON artifact_bindings(issue_ref);
CREATE INDEX idx_artifact_bindings_commit ON artifact_bindings(commit_sha);
