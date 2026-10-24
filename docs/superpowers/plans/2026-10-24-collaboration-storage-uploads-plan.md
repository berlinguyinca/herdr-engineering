# Collaboration Storage, Archives, and Uploads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move collaboration persistence to shared Postgres/S3-compatible storage, implement verified archive/restore, and support resumable high-volume uploads initiated through MCP by a registered harness.

**Architecture:** Keep room metadata and authorization in Postgres. Add an S3 abstraction around the existing RustFS client so live objects and immutable archives use configurable endpoints and prefixes. Let the harness upload bytes directly with temporary scoped S3 grants, while Herdr batches verification and registration.

**Tech Stack:** Postgres migrations, Python `asyncpg`/existing DB patterns, existing `herdr_engineering.control_plane.rustfs`, S3 multipart APIs, MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-collaboration-rooms-data-platform-design.md`

## Global Constraints

- Use separate `herdr-collab-live` and `herdr-collab-archive` buckets with opaque environment/room/archive prefixes.
- Do not create one bucket per room.
- Do not proxy terabyte-scale data through Herdr.
- Do not expose temporary S3 credentials in model-visible output.
- Unverified uploads are not visible as room files.
- A failed archive leaves the live room intact.

### Task 1: Add collaboration Postgres schema and repository boundary

**Files:**
- Modify: `deploy/control-plane/migrations/0003_collaboration_data_platform.sql`
- Create: `herdr_engineering/control_plane/collab_store.py`
- Modify: `herdr_engineering/control_plane/collab.py`
- Test: `tests/control_plane/test_collab_store.py`

**Interfaces:**
- `CollabStore.create_room(...) -> RoomRecord`
- `CollabStore.append_message(...) -> MessageRecord`
- `CollabStore.register_upload(...) -> UploadRecord`
- `CollabStore.list_room_files(...) -> list[RoomFileRecord]`
- `CollabStore.record_archive(...) -> ArchiveRecord`

- [ ] **Step 1: Write migration tests** that assert tables, unique constraints, foreign keys, and status checks for rooms, memberships, agents, permissions, uploads, files, archives, skills, and audit events.
- [ ] **Step 2: Run `pytest -q tests/control_plane/test_collab_store.py` and verify failure because migration/store do not exist.**
- [ ] **Step 3: Add the migration with UUID/text IDs matching existing collaboration IDs and `ON DELETE` behavior that cannot leave dangling access records.**
- [ ] **Step 4: Implement `CollabStore` as the only SQL boundary for new collaboration data.**
- [ ] **Step 5: Run the store tests against the repository’s configured Postgres test fixture.**
- [ ] **Step 6: Commit** `git commit -m "feat: add durable collaboration storage schema"`.

### Task 2: Add S3-compatible object-key and manifest service

**Files:**
- Create: `herdr_engineering/control_plane/collab_storage.py`
- Modify: `herdr_engineering/control_plane/rustfs.py`
- Test: `tests/control_plane/test_collab_storage.py`

**Interfaces:**
- `live_key(environment, room_id, file_id, variant=None) -> str`
- `upload_key(environment, room_id, upload_id, relative_path) -> str`
- `archive_key(environment, archive_id, name) -> str`
- `write_manifest(archive_id, manifest) -> ObjectRef`
- `verify_manifest(manifest, client) -> VerificationReport`

- [ ] **Step 1: Write failing key/manifest tests** for opaque IDs, stable prefixes, object hashes, and no email/room-name leakage.
- [ ] **Step 2: Run `pytest -q tests/control_plane/test_collab_storage.py` and verify failure.**
- [ ] **Step 3: Implement provider-neutral S3 configuration and key helpers; use the existing RustFS client only behind the adapter.**
- [ ] **Step 4: Implement manifest writing/checksum verification with deterministic canonical JSON.**
- [ ] **Step 5: Run focused tests and `git diff --check`.**
- [ ] **Step 6: Commit** `git commit -m "feat: add S3-compatible collaboration object storage"`.

### Task 3: Implement archive/delete/restore workflow

**Files:**
- Create: `herdr_engineering/control_plane/collab_archive.py`
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `herdr_engineering/control_plane/worker.py`
- Test: `tests/control_plane/test_collab_archive.py`

**Interfaces:**
- `archive_room(room_id, requested_by) -> ArchiveRecord`
- `restore_room(archive_id, requested_by) -> RoomRecord`
- `scrub_deleted_user(user_id) -> ScrubReport`
- `resume_pending_archives() -> list[ArchiveReport]`

- [ ] **Step 1: Add failing tests** for archive ordering, checksum failure preserving live data, verified deletion, new restore IDs, and user scrubbing of live/archive content while preserving room file bytes.
- [ ] **Step 2: Run `pytest -q tests/control_plane/test_collab_archive.py` and verify failure.**
- [ ] **Step 3: Implement the state machine `active → archiving → verified/deleted`, with durable idempotency keys.**
- [ ] **Step 4: Implement archive export for messages, session logs, permissions, agents, file metadata, files, and previews.**
- [ ] **Step 5: Implement restore into new relational IDs and new live prefixes.**
- [ ] **Step 6: Add worker retry/cleanup for failed archives and abandoned archive jobs.**
- [ ] **Step 7: Run focused tests plus existing worker tests.**
- [ ] **Step 8: Commit** `git commit -m "feat: archive and restore collaboration rooms"`.

### Task 4: Implement direct resumable upload sessions

**Files:**
- Create: `herdr_engineering/control_plane/collab_uploads.py`
- Modify: `herdr_engineering/control_plane/collab.py`
- Modify: `herdr_engineering/control_plane/collab_mcp.py`
- Test: `tests/control_plane/test_collab_uploads.py`
- Test: `tests/control_plane/test_collab_mcp.py`

**Interfaces:**
- `preview_upload(principal, room_id, pattern_manifest) -> UploadPreview`
- `start_upload(principal, room_id, manifest, options) -> UploadGrant`
- `get_upload_status(principal, upload_id) -> UploadStatus`
- `finalize_upload(principal, upload_id, checksums) -> list[RoomFileRecord]`
- `cancel_upload(principal, upload_id) -> None`

- [ ] **Step 1: Write failing tests** for permission checks, room-scoped grant prefixes, hidden-until-finalized files, checksum mismatch, retry/resume, and aggregated MCP status.
- [ ] **Step 2: Run `pytest -q tests/control_plane/test_collab_uploads.py tests/control_plane/test_collab_mcp.py -k upload` and verify failure.**
- [ ] **Step 3: Implement local manifest acceptance and short-lived scoped grant issuance using temporary credentials or presigned multipart URLs.**
- [ ] **Step 4: Implement finalize-time HEAD/checksum validation and one batched file-registration transaction.**
- [ ] **Step 5: Implement cancellation and worker cleanup for incomplete multipart uploads.**
- [ ] **Step 6: Run focused tests and the existing MCP/A2A security suite.**
- [ ] **Step 7: Commit** `git commit -m "feat: add resumable direct room uploads"`.
