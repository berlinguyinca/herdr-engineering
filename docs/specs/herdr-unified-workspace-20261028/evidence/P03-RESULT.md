# P03 result: artifact registry foundation (PARTIAL)

Status: **partial**. Backend foundation implemented and tested; share-link delivery, WebUI gallery and the CLI/Pi
`/artifact` adapter are not done. Design: `design/ARTIFACT_INTEGRATION.md`. Branch `feat/uw-artifact-registry-ext`
(base `origin/main` 28aff00b; migration 0029).

## Done (file evidence)
| Requirement | Where | Test |
|---|---|---|
| Provenance on every version (room/task/issue/project, parent, session, agent/model, prompt/skill revision, commit, redacted inputs) | `deploy/control-plane/migrations/0029_artifact_registry_extensions.sql`, `artifact_provenance.py`, `ArtifactStore.commit_version(origin=...)` | `test_provenance_is_validated_and_inputs_are_redacted` |
| Immutable versions, no deletes | trigger `artifact_versions_freeze` | `test_a06_v1_is_immutable_after_v2_and_rows_cannot_be_deleted` |
| Lookup by project / task / issue / room, ACL-filtered, 15 artifacts in 3 formats, survives DB restart and a deleted session | `artifact_context.py`, `artifact_registry_api.py` `GET /artifacts/for/{kind}/{ref}` | `test_a05_...survive_restart`, `test_lookup_routes_are_acl_filtered_and_missing_equals_forbidden` |
| No leak via counts, pagination, search, grants, expiry, revocation, missing-vs-forbidden | same SQL predicate as `list_records` | `test_a09_no_leak_...` |
| Two-phase publish ledger and reconcile of a simulated crash; duplicate events no-ops; orphan blobs marked | `artifact_publish_ledger`, `ArtifactLifecycleMixin.reconcile` | `test_reconcile_relinks_a_crashed_publish_once_...`, `test_one_artifact_links_to_many_tasks_and_duplicate_events_are_noops` |
| Resumable uploads | `artifact_uploads.py` | `test_resumable_upload_out_of_order_replay_verify_and_cleanup`, `test_resumable_upload_over_http_...` |
| Archive / restore / tombstone / purge with explicit policy | `artifact_lifecycle.py` | `test_archive_restore_tombstone_and_purge_respect_sharing_and_keep_evidence`, `test_lifecycle_routes_need_admin_...` |

## Not done / blocked
* Share links with TTL/revoke: `publication.py` is in-memory with write-through; delivery route and start-up reload are the
  P03b follow-up (see design doc). Existing publications behaviour is unchanged.
* WebUI gallery, version history/annotation UI (P06) and CLI/Pi `/artifact show|versions|diff|link|search|publish|share`.
* Room-derived read grants (needs P02 membership projection); this PR is owner + explicit grant.
* A scheduler calling `POST /artifacts/reconcile`; the route and store method exist.
* Gate "ten artifacts from two agents survive restart, rename and session deletion" is covered at the registry level
  (restart of PostgreSQL, no FKs on provenance); an end-to-end run through Pi is P05.
