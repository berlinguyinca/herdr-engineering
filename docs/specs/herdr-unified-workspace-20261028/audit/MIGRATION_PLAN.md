# P00 migration plan (additive, flag-gated, reversible)

Principles: additive tables/columns only; every migration idempotent with `-- DOWN:`; new feature flag `control_plane.unified_workspace.*` plus capability `unified_workspace_v1` in /api/v1/health so nav-capabilities hides new UI; old frontends unaffected (A18).

- P01: JSON Schema/OpenAPI contracts + fixtures in repo (no DB). Decide source-of-truth: project/team = new graph node types (`project`,`team`) in graph.py; issue ids resolve via github_issues alias table; task = durable graph node linked to collab room task (collab task stays the execution handle). Canonical id `urn:herdr:<kind>:<uuid>` with alias table.
- P02: migration >=0022: entity/alias/edge projection tables (uniqueness, source_event_id, tombstones), layout document table. Projection rebuildable from fabric_events (replay gate). Pi session ingest gains room_id/workspace_id/agent_instance_id.
- P03: add task_id (and project_id) to artifact_bindings; artifacts-by-X routes (ACL-filtered); two-phase publish reconcile; share link token+TTL on publication.py.
- P04-P05: Pi adapters, no schema.
- P06: UI only, capability-gated.
- P07: assistant participant adapter; approvals unify via a read-model over the 7 existing mechanisms (CONSOLIDATE, no new store), then external_action record.
- P08/P09: attention subscriptions, chaos, rollout.
Rollback per phase: flag off + capability unadvertised; DOWN comments drop only new tables.
