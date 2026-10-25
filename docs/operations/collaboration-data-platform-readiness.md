# Collaboration data platform: local completion report and deployment readiness

Branch `claude-collab-finish` (starts at `c847a18`). Nothing here was applied to a deployed database,
bucket or service; all database tests ran against a throwaway local Postgres.

## What is wired locally

HTTP adapters in `CollabService.dispatch` reuse the existing service methods and their authorization.
Each rejects unknown body keys, reports the returned record's real state, never falls back to the legacy
kick/removal path, and returns 503 when its service or event-loop runner is not injected.

| Route | Service method | Notes |
| --- | --- | --- |
| `POST /rooms/{r}/members/{u}/approve` | `decide_room_member` | owner only |
| `POST /rooms/{r}/members/{u}/upload-permission` | `set_file_upload_permission` | body is exactly a boolean |
| `POST /rooms/{r}/archive` | `archive_room` | 200 only when `verified` and `live_room_id` is null; `archiving` is 202, `failed` is 503 |
| `POST /archives/{id}/restore` | `restore_room` | always a new room ID |
| `POST /admin/users/{u}/delete` | `scrub_deleted_user` | operator only; legacy users get 409 until cutover |
| `GET /rooms/{r}/files?cursor=` | `CollabUploads.list_files` | finalized files only; keyset cursor; no storage keys |
| `GET /uploads/{id}` | `get_upload_status` (agent) / `get_room_upload_status` (person) | person sees counts only, for their own agent's batch or a room they own |
| `POST /rooms/{r}/files/{f}/access` | `CollabUploads.file_access` | 300 s https URL, forced `application/octet-stream`, audited |
| `POST /rooms/{r}/uploads/preview` | `preview_upload` | dry run, no side effects; 64 KB body cap (MCP allows larger for agents) |
| `GET /skills?scope=` | `search_skills` + `list_skill_drafts` | drafts only on the private tab |
| `GET /skills/drafts/{id}` | `get_skill_draft` | re-verifies the signed package; returns registry `publish_scopes` |
| `POST /skills/drafts/{id}/publish` | `publish_skill` | registry decides authority |
| `POST /skills/{id}/versions/{v}/download` | `download_skill` | 60 s https URL; agents refused (they use MCP) |

Dashboard parity: the operator listener calls the same `dispatch`; each new route family has a test that
compares operator and public responses.

## Required service injection (deployment blocker)

`cli.py` does not yet construct `CollabService` with any relational backend, so every route above answers
503 in a real deployment. A composition root must provide, on one owning event loop:

- `upload_service=CollabUploads(CollabStore(conn), CollabStorage(S3Config(...)))` and `upload_runner`
- `archive_service=CollabArchive(store, storage)` and `archive_runner`
- `skill_service=SkillTransfer(SkillRegistry(conn, journal_conn=..., ...))` and `skill_runner`
- `analysis_service`/`analysis_runner` (existing)

Configuration needed:

- **Postgres:** one asyncpg connection for the services plus a *separate autocommit* `journal_conn` for the
  skill package journal (`SkillRegistry` refuses the same connection). Each connection is single-use per
  operation: reads share the fail-fast lifecycle lock and may return 409 "busy" under contention.
- **S3:** `S3Config(environment, endpoint, region, live_bucket, archive_bucket, access/secret/session)`.
  Buckets must be distinct. Signature V4 is required. Browsers must be able to reach the endpoint over
  https (CORS) for file and skill URLs.
- **Skills:** an Ed25519 signing key, `key_id`, trusted public keys, and the maintainer user IDs.
- **Archive bucket must be unversioned.** `require_unversioned_archive_scrub` fails closed (including when
  versioning cannot be inspected), so person deletion refuses to run against a versioned archive bucket.
- **Migrations:** run `herdr-eng control-plane migrate` (0001-0009). Do not apply to production without review.

## Migration findings

`tests/control_plane/test_migrations.py` applies every prefix of the chain and the two historical 0003
edits, then the current chain: all succeed, a second run is empty, and the schema equals a fresh install.
Known gap (strict xfail): a database that applied the ten-minute first 0003 draft (`f5e1dad`) silently lacks
columns because `Migrator` skips by filename. Rebuild such a dev database or add an idempotent `0010`.

## Task liveness assessment (`tests/control_plane/test_collab_task_liveness.py`)

- After detach, member removal or agent revocation, an accepted task produces **no room-visible write** and
  never reaches an inbox; the only committed event is a denial audit. The task row lingers (displayed as
  accepted) until `TASK_TTL` (24 h). This is display-only; no spec change needed.
- Repeated `update_task` calls raise one owner prompt, not a stream. Removed members get 404 and no prompt.
- Bounded follow-up (not changed, pinned by an existing test): after an owner *revokes*, the agent's next
  `update_task` re-opens a pending request on the creator's behalf. Consider suppressing that until the
  user acts again.

## Cross-track contracts and their pinning tests

- Exact-agent upload ownership: `test_collab_upload_principals.py`, plus the new status tests.
- Verified-before-delete and new-ID restore: `test_collab_archive.py`
  (`...verifies_all_exports_before_deleting_live_data`, `test_deleted_restore_id_cannot_be_resurrected`,
  `test_http_lifecycle_routes_drive_the_real_archive_service`).
- Room-owned files survive user deletion: `test_scrub_removes_live_and_archived_authorship_but_preserves_files`
  and the HTTP lifecycle test.
- Skills signed, scoped, harness-only, never executed: `test_skill_registry.py`, `test_skill_transfer.py`,
  `test_collab_mcp.py`, `test_collab_http_skills.py`.
- Public/dashboard same source of truth: same `dispatch`, operator-vs-public tests in the new files.

## Not met locally / open

- No composition root (above); legacy-event-log rooms and users need a relational cutover before upload,
  archive, analysis or person deletion works (409 today).
- Preview/Flasheic async-job seam was not examined; no live Flasheic service was assumed.
- Guests cannot list or open files (conservative: `upload_permission` excludes guests).
