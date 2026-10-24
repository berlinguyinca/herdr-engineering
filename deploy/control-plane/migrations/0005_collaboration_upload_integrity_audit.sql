-- Native multipart SHA256 proves ordered parts, not a whole-file SHA256.
-- Keep asserted whole-file hashes in upload manifests, never in verified sha256.
ALTER TABLE collab_room_files ADD COLUMN verified_parts jsonb
    CHECK (verified_parts IS NULL OR (jsonb_typeof(verified_parts) = 'array' AND jsonb_array_length(verified_parts) > 0));
ALTER TABLE collab_room_files DROP CONSTRAINT collab_room_files_check;
ALTER TABLE collab_room_files ADD CONSTRAINT collab_room_files_check
    CHECK (status != 'available' OR sha256 IS NOT NULL OR verified_parts IS NOT NULL);

-- Prior Task 4 did not record whether HEAD was full-object or composite.
-- Conservatively downgrade its whole-file claims to the part evidence it checked.
UPDATE collab_room_files f SET sha256=NULL, verified_parts=item.value->'parts'
FROM collab_room_uploads u, LATERAL jsonb_array_elements(u.manifest->'files') item
WHERE f.upload_id=u.upload_id AND f.room_id=u.room_id AND u.manifest->>'schema_version'='1'
    AND item.value->>'relative_path'=f.relative_path AND jsonb_array_length(item.value->'parts') > 0;

-- Old cleanup completion did not prove live-copy multipart reclamation.
-- Recheck those sessions with the worker before allowing room archival.
UPDATE collab_room_uploads SET manifest=jsonb_set(manifest, '{cleanup_done}', 'false'::jsonb)
WHERE manifest->>'schema_version'='1' AND status IN ('available','failed');

ALTER TABLE collab_audit_events
    ADD COLUMN actor_kind text CHECK (actor_kind IN ('user','agent','system')),
    ADD COLUMN actor_agent_id text REFERENCES collab_agents(agent_id) ON DELETE SET NULL,
    ADD COLUMN outcome text CHECK (outcome IN ('complete','retry_required'));
