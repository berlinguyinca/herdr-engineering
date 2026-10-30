# Artifact integration: one registry (A) absorbs the collaboration data platform (B)

Status: design + P03 foundation implementation (flag-free, additive). Spec: `README.md` invariant 7 ("no overlapping
implementations"), `phases/P03.md`, `contracts/ENTITY_AND_EVENTS.md`, `security/THREAT_MODEL.md`.
Date: 2026-10-29. Neither side is in production, so either may change; this document chooses.

* **A** = the Artifact Registry on `origin/main` (`artifacts.py`, `artifact_acl.py`, `publication.py`, `rustfs.py`,
  `artifact_report*`/`artifact_preview.py`/`artifact_renderers.py`; migrations 0003/0004/0006/0008/0009/0010/0012/0021;
  routes `/api/v1/artifacts*`, `/api/v1/publications*`; agent contract in `~/IdeaProjects/herdr-artifacts`).
* **B** = the collaboration data platform in draft PR #366 (`collab_storage/uploads/archive/analysis/store.py`,
  `skill_registry.py`, `skill_transfer.py`, `packages/collab-files.js`, `collab-preview*.js`, vendored pdf.js;
  migrations 0022-0028).

## Principle

**One registry.** A owns artifact ids, immutable versions, metadata and sharing; RustFS/S3 owns bytes; Postgres holds
identity, authorization, provenance, state. A "room file" is not a second kind of thing: it is an artifact version that
carries `origin_room_id`. B's good ideas become *features of the registry*, never a parallel store.

## Bases, numbering, neighbours

| Item | Decision |
|---|---|
| Base of the registry-extensions PR | `origin/main` (28aff00b). It stores *native* ids (HCN task id, GitHub `issue_ref`, room id). `project_id` holds the P01 URN because a project has no native id. The URN for other kinds is derivable later with `workspace_contracts` uuid5, so no dependency on #364 (open, based on an older main) is needed and nothing in `workspace_contracts.py` is forked. |
| Highest migration | `origin/main`: **0030** (`inspector_snapshots`, #383; 0015-0019 gap stays unfilled: the `Migrator` applies unapplied files in name order, so backfilling would make fresh and existing databases differ). #366: 0022-0028 (will renumber when it lands). #364: none. A migration numbered below an already-applied one is an ordering hazard, so ours must be above main's highest. |
| Our numbers | **0031** `artifact_registry_extensions` (this PR). #376 added no migration. P02 takes a later number; re-check before merge. |
| #367 (open, "artifact ACL visibility, retraction") | It is the *graph-side* gate (`artifact_scope.py`) plus `ArtifactStore.record_retraction` (`lifecycle='retracted'`, version `state='retracted'`). We carve around it: we do not touch `artifact_scope.py`/`artifact_lineage.py`; our lifecycle states `archived`/`tombstoned` extend the same `artifacts.lifecycle` column and `retracted` stays valid (CHECK allows it). Lookup excludes every non-`active` lifecycle by default. If #367 merges first, only a one-line import/mixin rebase in `artifacts.py` is needed. |
| #364 | Contracts only; the lookup routes return rows that already carry the `{kind, id}` pairs `workspace_contracts` validates. |

## Feature table and decisions

| Capability | A (main) | B (#366) | Decision | Rationale / what happens to B's piece |
|---|---|---|---|---|
| Identity of a stored thing | `artifacts` + `artifact_versions` (immutable, CAS head, idempotency) | `collab_room_files` (file_id, room_id, relative_path) + `collab_analysis_artifacts` | **REUSE A** | B's room file = artifact version with `origin_room_id`. `relative_path` is a manifest entry name. |
| Immutability | Convention + CAS | Triggers on skills/analysis | **MERGE** | A gains a DB trigger: content + provenance columns of `artifact_versions` can never change (only `state`, `committed_at`, `purged_at`, `validation`, `job_id`); deletes forbidden. B's trigger idea, A's table. |
| Byte store | RustFS content-addressed `sha256/aa/bb/<hex>` (`put`, `put_stream`, verified `iter_get`) | `live/<env>/<room>/<file>` keys, archive prefixes, per-room layout | **REUSE A** | Content addressing dedups and makes hashes the integrity check. Per-room key layout dropped: location is metadata, not key structure. Staging keys (below) are the only non-CAS keys. |
| Bytes path for reads | Control-plane streams (`/download`, preview) | Presigned GET to browser | **REUSE A** | Presigned GET needs a browser S3 origin => CSP hole. Streamed reads keep CSP at `'self'`. |
| Direct-to-S3 browser upload | none (single POST, 64 MiB-ish bodies) | Presigned multipart, `connect-src https:` | **DROP for now / future option** | See "CSP" below. Resumable uploads are re-expressed as *proxied* sessions. |
| Resumable / multipart upload | none | `collab_room_uploads` + `_parts`, composite SHA-256, resume, expiry, cleanup | **MERGE** | `artifact_upload_sessions` + `artifact_upload_parts` in the registry. Parts staged at `staging/uploads/<upload_id>/<n>`; finalize streams parts into one verified `put_stream`, registers a scoped blob, deletes only staging keys. Digest checked per part and whole file. Expiry sweeper. |
| Archive / restore | none (lifecycle column unused) | Room archive: export manifest, verify, delete live, restore, scrub user | **MERGE (artifact-level)** | `artifacts.lifecycle` becomes a checked state machine `active <-> archived`, `active/archived -> tombstoned -> purged(bytes)`, plus #367's `retracted`. Append-only `artifact_lifecycle_events`. Room-level archive/restore (messages, memberships) is HCN's, not the registry's; it calls the artifact lifecycle for the room's artifacts. B's export manifest format is not adopted (a version row *is* the manifest). |
| Deletion policy | none | Verified deletion + user scrub | **MERGE** | Explicit policy below: no row is ever deleted; bytes purge only after tombstone + hold window + zero remaining references; every step is an audit event. |
| Previews | `artifact_preview.PreviewPolicy`, renderers, sandboxed HTML/SVG, report preview route | Client-side parsers (pdf.js worker, text, MSP, FlashEIC) with CSP changes | **REUSE A; DROP B's CSP; keep B's idea as typed classifier** | A already serves previews with `nosniff`, `default-src 'none'`, sandbox for HTML/SVG. Registry gains `preview_kind()` (pdf, image, markdown, json, html(sandboxed), doc/spreadsheet/presentation (download + converted PDF when a renderer exists), plot-data, log, url) surfaced in lookup rows. pdf.js vendoring / `frame-src blob:` is not taken: revisit only as a separate, reviewed UI PR under its own CSP. |
| Provenance (chat analysis) | `provenance` jsonb, agent/host/session/mission/job columns (FKs) | `collab_analysis_requests` (prompt, files, parent artifact, tool, runtime) | **MERGE** | Typed, indexed provenance columns on `artifact_versions` (plain text, **no FKs**, so it survives session/agent deletion): origin room/task/issue/project, session ref, agent ref, model, prompt revision, skill revision, commit, redacted inputs. B's "harness-reported, not attested" wording is kept. |
| Linking to work objects | `artifact_bindings` (mission/session/agent/stage/room/issue/commit), no task/project | Room membership only | **EXTEND A** | `task_id`, `project_id`, `link_kind`, `version_id`, `source_event_id`; dedupe index rebuilt; ACL-filtered lookup routes. |
| ACL | `created_by` + `artifact_acl` grants (read/edit/review/share/admin, expiry), in-memory `ArtifactAcl` write-through to Postgres | Room-membership based file permissions in `CollabService` | **REUSE A; room membership is an input, not a store** | One grant table. Room-derived read (members see room artifacts) is a *decision for P02/P06*: it must be expressed as a grant written when an artifact is linked to a room (so revocation is a delete), never as a second ACL evaluation path. This PR ships owner + explicit grant only. |
| Lifecycle permissions | Admin permission on artifact | Owner/maintainer rules per lifecycle op | **REUSE A** | archive/restore need `admin`; tombstone needs `admin` + reason; purge is a system operation (operator role), never user-triggered. |
| Sharing | `publications` (pinned version, token hash, TTL, approval, revoke, providers; audience `unlisted_link`/`authenticated_guest`/`public_discoverable`) | Presigned URLs, 60 s transfer capabilities | **REUSE A** | Share links = publications (already immutable-version-pinned with TTL and revoke). Gaps listed below; not in this PR. |
| Skill registry + transfer | none | `skill_registry.py`, `skill_transfer.py`, Ed25519-signed packages, `collab_skill_*` tables | **EXTRACT (separate PR)** | Skill bundle = registry artifact (`artifact_type='skill'`), signature/manifest in provenance, 60 s capability served via control-plane download. `collab_skill_*` tables are dropped. |
| Chat-analysis requests | n/a | `collab_analysis_*` | **DROP table, keep fields** | Request coordination is HCN/Pi work (P05). The provenance it records moves onto the version (above). |
| Web client for files | `artifacts-view.js` | `collab-files.js`, `collab-previews.js` | **DEFER (P06)** | WebUI is inspection only per the spec; it will call the lookup routes. Not part of the backend foundation. |
| Reports/review/comments/checks | Full (0021) | none | **REUSE A** | Unchanged. |
| Storage config | `rustfs_*` keys | `S3Config`, bucket per env | **REUSE A** | One bucket, one client. |

Everything B has that is **not** listed above (room/membership/agent tables 0022, messages, invitations, session logs, MCP
tools) is HCN's domain and out of scope here.

## Data model (migration 0031, additive, idempotent, `-- DOWN:`)

`artifact_versions` (all nullable, **no foreign keys**: the P03 gate requires survival of session deletion, and the
existing `agent_id/host_id/session_id/mission_id` columns *are* FK-bound):
`origin_room_id, origin_task_id, origin_issue_ref, origin_project_id, origin_session_ref, origin_agent_ref, model_id,
prompt_revision, skill_revision, commit_sha, redacted_inputs jsonb, purged_at`. Parent version already exists
(`parent_version_id`, FK within the registry). A BEFORE UPDATE/DELETE trigger freezes content and provenance.

`artifact_bindings` (used **only for lookup**; per-version provenance lives on the version): `task_id, project_id,
link_kind ('origin'|'reference'|'derived'), version_id (first version that created the link, no FK), source_event_id`.
The old dedupe index `(artifact_id, mission_id, COALESCE(session_id,''), COALESCE(stage,''))` is replaced by one that
includes `link_kind, task_id, project_id, issue_ref, room_id` so one artifact can be linked to several tasks; a partial
unique index on `(artifact_id, source_event_id)` makes duplicate events no-ops.

`artifacts`: `lifecycle` CHECK in (`active`,`archived`,`retracted`,`tombstoned`), `archived_at`, `tombstoned_at`,
`purge_after`. `artifact_lifecycle_events` (append-only, `request_key` unique = idempotent).

`artifact_publish_ledger`: one row per committed version: `source_event_id` (deterministic `art-<version_id>`), `state`
(`committed` -> `linked`), `attempts`, `last_error`. `artifact_blobs.orphan_marked_at`.

`artifact_upload_sessions` / `artifact_upload_parts`: see upload section.

## Provenance fields required by P03

| P03 requirement | Column / place |
|---|---|
| origin room / task / issue / project | `origin_room_id`, `origin_task_id`, `origin_issue_ref`, `origin_project_id` (+ a binding row each) |
| parent artifact version | `parent_version_id` (existing; also `base_version_id`) |
| execution session | `origin_session_ref` (plain) and existing `session_id` (FK, only if the row exists) |
| agent / model | `origin_agent_ref`, `model_id` |
| prompt / skill revision | `prompt_revision`, `skill_revision` (e.g. `skill_id@version`) |
| repository commit | `commit_sha` |
| inputs with privacy redaction | `redacted_inputs`: list of `{name, kind, sha256, size_bytes, redacted}`; values under keys like token/secret/password/authorization/cookie/api_key and well-known token shapes are replaced by `[redacted]` server-side before storage; raw prompts are never stored, only a digest |
| content hash / version | `output_manifest[].sha256`, `version_number` (existing) |

`origin` is validated server-side (length, charset, no control characters). `created_by` and the principal are derived
from authentication (existing rule); the caller cannot forge another principal. Model/agent/prompt are **self-reported
by the harness, not attested** (B's wording retained).

## API (additive, `/api/v1/artifacts`)

| Route | Permission | Notes |
|---|---|---|
| `GET /artifacts/for/{kind}/{ref}` (`kind` in `project|task|issue|room`, `ref` percent-encoded) | authenticated; rows filtered by read ACL | params `q`, `format`, `limit` (<=200), `cursor`. Response `{kind, ref, artifacts:[...], total, has_more, next_cursor}`; `total`/`has_more` are over *visible* rows only |
| `GET /artifacts/{id}/links` / `POST .../links` | read / edit | list and idempotently add bindings (`source_event_id` dedupes) |
| `POST /artifacts/{id}/versions` | edit (existing) | accepts `origin{...}`; writes provenance, bindings, ledger in the same transaction as the commit |
| `POST /artifacts/{id}/uploads` -> `upload_id` | edit | declared `sha256`, `size_bytes`, `media_type`, `part_size`; idempotent by `Idempotency-Key` |
| `POST /artifacts/{id}/uploads/{uid}/parts/{n}` (raw bytes) | edit | part sha256 header verified; replays of the same part are no-ops, different bytes = 409 |
| `GET /artifacts/{id}/uploads/{uid}` | edit | received parts for resume |
| `POST /artifacts/{id}/uploads/{uid}/complete` / `abort` | edit | complete verifies size + whole-file SHA-256 then registers the scoped blob |
| `POST /artifacts/{id}/archive` / `restore` / `tombstone` | admin | `{reason, request_key}` |
| `POST /artifacts/reconcile` | operator role | re-emits missing lineage, marks orphan blobs, expires upload sessions, purges due tombstones |

Existing `GET /artifacts`, `/artifacts/{id}`, download and report routes are unchanged. Missing and forbidden are the
same `404 {"error":"not found"}`.

## ACL mapping

Visibility predicate = the one `list_records` already uses: `created_by = principal` OR an unexpired `artifact_acl`
row holding `read` or `admin`. It is applied **inside SQL before `LIMIT`, counts, `has_more` and search**, so pagination,
totals and `q` never describe rows the viewer cannot read. The legacy "unowned artifacts are readable by all" rule and
the in-memory legacy projection merge are **not** applied to context lookups (they are a leak path). A nonexistent task
and a task whose artifacts are all invisible return the identical empty page. Write-through of grants is existing
(`ArtifactAcl.on_change -> record_acl_event`), so the SQL view and the in-memory `_artifact_allowed` agree after restart
(`load_acl`). Roles (unified workspace broker, #364) intersect on top: the control-plane role gate stays in front of
every route; a read-only room grant never reaches a mutation route.

Mapping from the unified-workspace broker actions: `read_artifact` -> `read`; `comment`/`annotate` -> `review`;
`publish_artifact` -> `edit`; `share_artifact` -> `share`; `archive/tombstone` -> `admin`. Guests: a time-boxed
`artifact_acl` row (expiry) or an `authenticated_guest` publication; no guest ever gets a context route.

## Two-phase publish and reconciliation

1. **Stage binary**: bytes go through the scoped upload (`POST .../blobs` or an upload session). A blob is verified
   (`artifact_blobs.verified_at`) and unreferenced.
2. **Metadata**: `commit_version` (single transaction): CAS on the head, version row with provenance, bindings, ledger row
   `state='committed'`, idempotency record, audit row. Retrying with the same `Idempotency-Key` returns the original.
3. **Graph edges**: after commit the route calls `emit_version_lineage` (deterministic event ids `art-<version>-<n>`),
   then `mark_linked(version_id)`. A crash between 2 and 3 leaves `committed` in the ledger.
4. **Mark available**: `artifacts.status='available'`/head move happen in step 2's transaction; nothing is visible before.

`reconcile` (idempotent, safe to run concurrently thanks to `FOR UPDATE SKIP LOCKED` on ledger rows):
`committed` older than a grace period -> re-emit lineage (same event ids => duplicates dedupe at ingest) -> `linked`;
blobs verified but referenced by no version for > TTL get `orphan_marked_at` (bytes are never deleted by reconcile);
upload sessions past `expires_at` are aborted and their staging parts deleted; tombstones past `purge_after` are purged.
Duplicate events: bindings by `source_event_id`, lineage by event id, commits by idempotency key.

## Resumable uploads

Sessions live in the registry, are scoped to the artifact (edit permission), and expire (default 24 h). The control plane
proxies parts: no browser-to-S3 traffic, so **no CSP change**. Limits: part <= 64 MiB, <= 10,000 parts,
declared total size <= 1 GiB (same ceiling as scoped blob upload). Finalize streams parts in order through `RustFSClient.put_stream(expected_sha256,
expected_size)`, so a bad byte anywhere fails the whole upload and nothing lands in the content store.

## Lifecycle and deletion policy

* `archive`: hide from default lists and the lookup routes; bytes and versions untouched; reversible by `admin`.
* `tombstone`: requires a reason; sets `purge_after = now + hold` (default 30 days; legal hold = `purge_after` NULL);
  restorable until purged. Share links on a tombstoned artifact stop delivering immediately.
* `purge` (reconcile, operator): deletes object bytes **only** if the blob's `storage_key` is referenced by no other
  non-purged version or scoped blob (content addressing means keys are shared); marks versions `purged_at`/`state='purged'`.
  Version rows, hashes, provenance and the audit trail remain forever ("append-only evidence, explicit deletion policy and
  tombstones"). Nothing issues `DELETE` on registry rows (the trigger enforces it).
* `retracted` (from #367) is a visibility withdrawal that keeps bytes; it never purges.

## Share links with TTL / revoke

Reuse `publication.py` (`publications` table: pinned version, `token_hash`, `expires_at`, `revoked_at`, approval,
audience, provider). Not in this PR; the follow-up (P03b) must: (1) persist records at start-up (the controller is
in-memory with write-through today); (2) add a delivery route `GET /api/v1/s/<token>` that resolves the hash, checks
state/expiry/guest, and streams only the pinned version through `PreviewPolicy` headers; (3) make revoke effective on
read (re-check state on every request, no caching); (4) require explicit approval for `public_discoverable`; (5) refuse
links to `archived/tombstoned/retracted` artifacts. Tokens never appear in logs or graph attributes.

## CSP and direct-to-S3

#366 sets `connect-src 'self' https:` and `frame-src 'self' blob:`. **We do not copy that**: `connect-src https:` lets any
injected script exfiltrate to any HTTPS host. This design needs no browser origin other than `'self'` because uploads and
downloads are proxied. If direct-to-S3 is wanted later (very large files), the only acceptable form is: presigned
`UploadPart` URLs for the single configured RustFS origin, with `connect-src 'self' <origin>` where `<origin>` comes from
YAML `control_plane.artifacts.upload_origin` (scheme + host + port, validated, https or loopback only), emitted by the
server only when set, with bucket CORS restricted to the dashboard origin. Presigned URLs are 60 s, per-part, never logged.

## What happens to #366

Superseded (do not merge as-is; the owner decides): `collab_storage.py` key layout, `collab_uploads.py` (re-expressed on
the registry), `collab_archive.py` file handling, `collab_analysis.py` tables (fields moved to versions),
`skill_registry.py`/`skill_transfer.py` (extracted to `feat/skill-registry` on registry artifacts), the file/preview web
client and vendored pdf.js (deferred), and the CSP edits (rejected). Kept by HCN: rooms, memberships, invitations, agent
access, messages, session logs and their migrations 0022 (non-file parts).

## Test mapping (acceptance matrix)

* A05: 15 artifacts in 3 formats (pdf, markdown, json) linked to project/task/issue are found by each route, versions stable.
* A06: v2 committed after v1; v1 row cannot be updated or deleted (trigger); v1 content readable and hash unchanged.
* A09: another principal sees no artifact, no count, no `has_more`, no search hit; missing == forbidden == empty.
* P03 gate: ten artifacts from two agents survive restart/rename/session deletion (no FK); reconcile repairs a simulated crash.

## Partial / not done here

Share-link delivery route and publication persistence (P03b); room-derived read grants (P02/P06); WebUI gallery; CLI
`/artifact` adapter commands; a scheduled reconcile caller (the route and store method exist; a timer is deployment).
