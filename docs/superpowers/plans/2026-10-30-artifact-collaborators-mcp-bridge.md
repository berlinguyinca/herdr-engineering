# Artifact collaborators over the collab MCP bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 3 of the artifact sharing / collaboration design. The owner grants collab agents an artifact; those agents read it and commit to it through seven new tools on the collaboration MCP server. The spec says six. The seventh, `artifact_stage_chunk`, is an operator-approved deviation; see Decisions. The Share panel gains Collaborators and Activity tabs.

**Architecture:** `collab-public` has no database or registry, so the bridge is three directories:
* **Mirror** (`grants.json` with the published `limits`, `artifacts/<id>/head.json`, content-addressed `blobs/<sha256>`, `results/<ticket>.json`). Only the worker writes it, and it is mounted read-only into `collab-public`.
* **Outbox** (`intents/<ticket>.json`, `agents.json`, `staging/<owner_key>/<upload_id>/`). `collab-public` writes intents, `agents.json` and staging (parts, `meta.json`, its `audited` marker). The worker consumes it: it writes only the `verified` marker into an upload dir and deletes consumed intents, verified parts and swept staging. The outbox is a tree `collab-public` controls and the worker runs as root, so every worker write or delete inside it goes through the fd-relative, no-follow helpers of Task 3 (`write_marker_beneath`, `unlink_beneath`, `remove_tree_beneath`), never a path-based call.
* **Spool** (worker only): `put_stream`'s temp files during assembly.

File content of any size or type travels as hash-checked chunks staged in the outbox. The worker assembles them and verifies them against the declared sha256 and size, streaming them into RustFS. A commit then references the staged upload by `upload_id`. Small text can still go inline in the commit.

A new worker loop, `CollabBridgeLoop`, runs on a ~1 s tick and re-checks everything against Postgres. It runs as three asyncio tasks with their own asyncpg connections:
* a grants task (`publish_grants`, invite resolution, retraction refresh) on a fixed 1 s cadence that never waits on a mirror refresh;
* a mirror task (`mirror.sync`, staging and result sweeps), which may take long for a large artifact;
* an applier (intents) with per-lane concurrency, so that one large assembly never stalls the `grants.json` heartbeat or other agents.

The applier checks:
* the agent, from `agents.json`;
* the grant, from `artifact_acl` rows for `collab:<agent_id>`;
* retraction, from the durable state and a worker-local gate.

It assembles and stores staged uploads and stores inline text. It then calls the already-built `ArtifactStore.merge_commit` (in no outer transaction, passing the ticket so the attempt row links to it), records the outcome, writes the result file, refreshes the mirror and posts lineage. Owner routes on the dashboard manage grants and serve the Activity data through a `SyncArtifactAdapter` over `CollabGrantStore`. Grants are DB-only and never enter the web process's in-memory ACL.

**Tech Stack:** Python 3.12 / asyncpg (control plane, worker), stdlib file I/O (`os.open` + `O_NOFOLLOW` + `dir_fd`, rename-into-place, `os.link` for no-replace), plain ES-module JS (`packages/`), node:test, Playwright, Postgres migrations in `deploy/control-plane/migrations`, docker compose overlays and nginx in `deploy/control-plane`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md`:
* section B (including the merge rules, scope and limits; the limits are superseded as recorded under Decisions);
* section C;
* "Builder-check resolutions" and "Second builder check" (authority split, `collab:<agent_id>` ACL rows, 60 s intent age, `agents.json`, mirror = head + previous 5 versions' text files plus all manifests).

The merge engine (plan 2) is in `herdr_engineering/control_plane/artifact_merge.py` and `ArtifactStore.merge_commit` (`artifacts.py:357`). Its precedents:
* share-ingest: `artifact_shares.ShareStore.ingest_and_rotate_events`, `share_public.EventLog` (share_public.py:228) / `share_public._open_beneath` (share_public.py:302);
* MCP: `herdr_engineering/control_plane/collab_mcp.py`;
* verified streaming into the store: `RustFSClient.put_stream(expected_sha256, expected_size)` as used by `artifact_uploads.complete_upload`; streaming reads: `RustFSClient.iter_get(key, sha)` (rustfs.py:115);
* web wiring of a DB store: `cli.py:_share_wiring` (a `SyncArtifactAdapter` over `ShareStore`);
* bounded text diff: `ArtifactBundle.diff(left, right, *, media_type, max_lines)` (artifact_bundle.py:36).

Pre-flight scan: `.superpowers/sdd/2026-10-30-artifact-collaborators-mcp-bridge/preflight.md`. Its rulings R1–R17 are applied in this text.

## Decisions (operator, 2026-10-30)

These decisions are binding and supersede the spec where they differ.

1. **Chunked commit staging instead of a large `/mcp` body.**
   * The public `/mcp` body cap stays at 64 KiB for every tool except `artifact_stage_chunk`. That one tool alone gets a modest raise to 768 KiB, and only for an authenticated agent holding a write grant.
   * Content is staged in 512 KiB chunks (raw). Staging is resumable and idempotent per chunk. The worker assembles each upload and verifies its sha256 server-side, and a sweeper bounds the staging space.
   * This makes **seven** tools where the spec says six. That deviation is operator-approved.
   * The spec's "≤ 2 MiB total per commit" is gone. `max_text_merge_bytes` (default 2 MiB) now applies only to three-way **text merging** (`merge_commit(max_text_bytes=…)`).
2. **Large and binary files are writable through staging.** This is also an approved spec deviation: the spec's non-goals say "not writing binary files via collaboration in v1".
   * Such files take the merge engine's whole-file path, which is already built. A change on one side only is taken as is.
   * When both sides changed, the file becomes a conflict of kind `binary` (when not text-like) or `oversize` (over `max_text_merge_bytes`), with `merged_text: null`.
   * Report and renderer-built artifacts stay read-only for collaborators.
3. **Daily byte quota is 100 GB per agent.** That is decimal: `daily_bytes_per_agent: 100000000000`. It is set in the YAML fleet config and read by no environment variable. The bytes-quota mechanism stays; only the value is configurable.
4. **No tight size caps.** Storage is plentiful (hundreds of TB). Every size, count and rate limit is a configurable sanity or anti-abuse limit with a generous default (see Global Constraints → Configuration). The fixed protocol constants are the chunk size and the chunk body cap, because plan 5 shares them.
5. **Internal (non-external) agents may receive write grants.**
6. **Grant lifetime:** the default is 30 days and the maximum is 90.
7. **Retraction fail-safe.** When a retraction is hidden in the web process but fails to persist (`retract` returns `persisted: false`), the route **also revokes every collaborator write grant on that artifact**, for an artifact or a version retraction alike. Read grants are untouched.
   * **Restrict all access is optional and operator-driven, never automatic.** When the retraction fails to persist, the response offers "restrict all access now?". The operator can accept through `herdr-eng artifacts retract --restrict-all` (a new command, Task 11), the **Restrict all access** button in the Collaborators tab, or `POST /api/v1/artifacts/{id}/collab-restrict`. That action also revokes the read collaborator grants, so the mirror drops the artifact and collaborators no longer see it. Without that action, read collaborators keep seeing the artifact until the operator retries the retraction.
8. **Hosting.** The MCP endpoint stays on its existing host and path. The member/viewer UI on `artifacts.metabolomics.us` belongs to plans 4 and 5. Nothing in this plan changes for it.

Standing operator orders that apply throughout: YAML configuration only (no environment variables read by code); never use iframes; no secrets in logs.

## Global Constraints

* **Principal and identity**
  * A collaborator's principal is exactly `collab:<agent_id>`. It is used as `created_by` for `merge_commit`, and the idempotency namespace follows from it. `collaborator_agent_id=<agent_id>`.
  * Grants are `artifact_acl` rows for `collab:<agent_id>`. Permissions are `["read"]` (read) or `["read","edit"]` (write), and `expires_at` is required. The default is 30 days and the maximum is 90.
  * Internal and external agents may both hold write grants.
  * A pending invite grant is never an `artifact_acl` row.
* **Tools and the MCP endpoint**
  * The seven tools are `artifacts_list`, `artifact_head`, `artifact_read`, `artifact_history`, `artifact_stage_chunk`, `artifact_commit` and `artifact_commit_result`.
  * Every tool is validated by hand (`additionalProperties: False` in the schema and an unknown-argument check in code).
  * Every tool result is wrapped `untrusted=True`, including the commit-ticket reply and the stage-chunk reply (spec B).
  * `tools_for` offers the tools only to an agent with at least one live grant in a fresh `grants.json`.
  * Body caps:
    * `MAX_BODY` (64 KiB) is unchanged for every tool and route.
    * `MCP_UPLOAD_MAX_BODY = 786_432` (768 KiB) applies to `artifact_stage_chunk` only, and only for an authenticated agent holding a write grant.
  * Request-rate ceilings on `/mcp` (Task 8b). These are fixed edge sanity ceilings per client IP, sized so the per-agent limits below are reachable:
    * edge nginx: `location = /mcp` has its own zone `hcn_mcp` at `rate=25r/s`, `burst=100 nodelay` (the server-level `hcn_req` 10 r/s keeps applying to every other location; `limit_conn hcn_conn 20` still applies);
    * control-plane server: `POST /mcp` goes to its own `_mcp_limiter = _RateLimiter(max_requests=1800)` (per client, 60 s window) instead of the shared 600/min `_limiter`, so the edge stays the binding per-IP ceiling.
    * The effective per-IP ceiling is therefore 25 requests/s (about 12.5 MiB/s of chunks), shared by every agent behind one IP. The per-agent `chunks_per_minute` (default 1200 ≈ 10 MiB/s) and `commits_per_minute` are the binding anti-abuse limits.
* **Commits and staging**
  * A commit change is either:
    * `{name, op: "put", text}` with inline UTF-8 text, bounded in practice by the 64 KiB body;
    * `{name, op: "put", upload_id}`, which references a complete staged upload of any size or type under the **caller's own** staging dir;
    * `{name, op: "delete"}`.
  * Chunks are `CHUNK_BYTES = 524_288` (raw), sent base64 with their own sha256. Every chunk except the last is exactly `CHUNK_BYTES`.
  * An upload declares `name`, `size_bytes` (≥ 1; an empty file is inline `text: ""`) and the whole-file `file_sha256` on every chunk, and `total` must equal `ceil(size_bytes / CHUNK_BYTES)`.
  * `upload_id` is derived, never chosen: `"upl_" + sha256(owner_key | artifact_id | name | file_sha256 | size_bytes)[:24]`, where a collaborator's `owner_key` is its agent id. Re-sending the same file therefore resumes the same upload.
  * A chunk is written to a temp file and then `os.link`ed to its final name, so its final name never holds partial content and an existing chunk is never replaced. The same index with the same sha replays as success. The same index with a different sha is refused with 409.
  * Write grants are for plain file artifacts only: not `artifact_type == "report"`, no `report_descriptor` and no `source_manifest_blob` on the head. Read grants are allowed for any artifact.
  * Quota accounting:
    * The quota counts inline text bytes, from the ledger, plus each staged upload's size **once**, when the worker first verifies it (`artifact_collab_uploads`).
    * The check runs **before** assembly, using the declared sizes of not-yet-verified uploads, and excludes the current ticket's own `received` row (its inline bytes are added explicitly), so a crash-retry is never counted twice.
    * A re-commit of an already verified upload, for example after a conflict, costs nothing more and is never re-assembled.
* **Intent validation in the worker**
  * The worker rejects an intent older than 60 s at first receipt (`submitted_at`). The age check runs at scan time, before lane wait, so an intent queued behind its own agent's long assembly never expires once its `received` row exists.
  * It fails closed when `agents.json` is older than 120 s, and `collab-public` fails closed when `grants.json` (and so `limits`) is older than 120 s or missing.
  * A change's allowed keys are `{name, op, text, upload_id}`. Any `sha256`, `storage_key`, `blob_sha256`, `entry`, `size_bytes` or `media_type` key in a change is rejected. Content identity comes only from inline text or from bytes the worker assembled and verified itself, never from a caller-supplied hash or key.
  * Every name passes `share_bundle.normalise_name` on both sides. A staged upload's declared `name` and `artifact_id` must equal the change's name and the intent's artifact.
  * An `idempotency_key` replays a prior decided ticket only when the prior ticket's `request_sha256` equals this one's; otherwise the ticket is `rejected` "idempotency key was reused for a different request" (matching `merge_commit`).
* **Retraction**
  * A commit is refused if any of these holds:
    * the artifact's `lifecycle == 'retracted'` (checked inside `merge_commit`);
    * the base or head version has `state = 'retracted'`;
    * a worker-local `ArtifactGraphGate`, loaded with `load_retractions()` at least every 30 s, says the artifact, base or head is retracted.
  * The mirror never contains a retracted artifact or version.
  * When a web-side retraction fails to persist, the retract route revokes every `collab:` write grant on that artifact (Decision 7, Task 10b).
  * Restricting all access (read grants too, hiding the artifact from collaborators) is a separate operator action, never automatic (Decision 7, Task 10b).
  * The residual is that the revoke itself can fail. If it fails because Postgres is unreachable, the worker's `merge_commit` fails too. The route reports a failed revoke and never claims success.
* **`merge_commit` contract**
  * Always called without an outer transaction, so its `artifact_collab_attempts` row persists. It is called with `max_text_bytes = max_text_merge_bytes` and `ticket_id = <ticket>` (Task 5b adds the keyword; default `None` keeps plan 2's callers unchanged).
  * `ArtifactMergeConflict` gives result `conflict`. It carries `head_version_id`, `attempt_id` (Task 5b), `{name, kind}` per file and `merged_text`, which is non-null only for `modify_modify_text`. All `merged_text` in one result is capped at `max_conflict_text_bytes`; over the cap a file gets `merged_text: null, truncated: true`.
  * Any other `ArtifactError` → result `rejected` with its message.
* **No secrets anywhere.** These never appear in intents (except the text itself), staging metadata, results (except conflict text), mirror metadata, worker/server logs or audit events:
  * agent credentials and `Authorization` values;
  * invite codes and share tokens;
  * committed text and staged bytes.

  Logs name only the ticket or upload id, agent id, artifact id and outcome.
* **No public links from collaborators.** No tool, intent kind or route reachable by a `collab:` principal creates, approves or lists a share. Generic ACL grants refuse `collab:` and `collab-invite:` principals.
* **Intent kinds and actors (extension points for plans 4 and 5).**
  * Intents are dispatched through a kind registry. Plan 3 registers exactly one kind, `commit`, and an unregistered kind is rejected; `share_create` stays rejected.
  * Every intent carries an `actor`: `{kind: "agent", id: <agent_id>}` or `{kind: "user", id: <user_id>}`. The `commit` kind accepts only agent actors and always runs as `collab:<agent_id>`.
  * Kinds that other plans register run their own authorization, as their own principals (plan 5: `member:<user_id>`). See "Contract for plan 5".
* **Audit**
  * Every commit submission goes through the collab event log as `artifact_commit_submitted`. Every denial goes through the existing `service._audit_denied` (which commits `audit_denied`); there is no artifact-specific denial event type.
  * Allowed reads are coalesced: at most one `artifact_tool_used` per (agent, artifact, tool) per 600 s, as `TERMINAL_AUDIT_EVERY` does. Each coalesced event carries a `calls` count, so every call is accounted for.
  * Chunk staging is audited once per upload: an `artifact_upload_staged` event when an upload is first observed complete (by the stage-chunk scan or by `commit()`'s precheck), carrying upload id, artifact and size, never content. "Once" is enforced by an `audited` marker created with `link_new_bytes` in the upload dir.
  * Exactly three new event types join `BULK_EVENTS`: `artifact_tool_used`, `artifact_commit_submitted`, `artifact_upload_staged`.
* **Migrations.** Plan 3 takes `0045_artifact_collab_bridge.sql`. origin/main already holds `0043_artifact_version_parents.sql`, `0043_assistant_apps.sql` and `0044_assistant_reminders.sql`; plan 4 (annotations) takes 0046 and plan 5 (member-owned artifacts) takes 0047. Re-check `git fetch origin; git ls-tree origin/main deploy/control-plane/migrations/ --name-only` right before committing and renumber (here and in plans 4/5) if main has taken it. Migrations must be additive and idempotent.
* **Configuration and conventions**
  * Configuration is YAML only. No environment variables are read by the code. Everything lives under `control_plane.collab_artifacts`, which is off unless `enabled` is exactly `true`.
  * Compose paths come from `deploy.sh` exports, as the share dirs do.
  * `BridgeLimits.from_mapping` (Task 3) validates every limit: positive ints, and a config with `0`, a negative value or a bool is refused at load. Defaults:

    | Key | Default | Purpose |
    |---|---|---|
    | `daily_bytes_per_agent` | `100000000000` (100 GB) | quota |
    | `commits_per_minute` | `30` | anti-abuse |
    | `chunks_per_minute` | `1200` | anti-abuse (~10 MiB/s per agent; within the 25 r/s per-IP edge ceiling) |
    | `max_changes_per_commit` | `100` | sanity |
    | `max_pending_intents_per_agent` | `4` | sanity |
    | `max_file_bytes` | `100000000000` | per staged file; sanity, implied by the quota |
    | `staging_ttl_seconds` | `86400` | sweeper |
    | `staging_bytes_per_agent` | `200000000000` | sanity, reserved by declared size |
    | `staging_bytes_total` | `2000000000000` | sanity, whole volume |
    | `max_text_merge_bytes` | `2097152` | three-way text merge only |
    | `max_conflict_text_bytes` | `8388608` | per result |
    | `mirror_history_versions` | `5` | mirror |
    | `mirror_max_manifests` | `100000` | mirror (effectively "all versions") |
    | `mirror_max_file_bytes` | `1073741824` | per mirrored text file |
    | `mirror_max_artifact_bytes` | `17179869184` | per mirrored artifact |
    | `apply_concurrency` | `4` | applier lanes |

  * The worker publishes the effective limits in `grants.json` (`limits`), so that `collab-public` enforces the same values with no limit configuration or limit flags of its own. The worker re-enforces every limit from its own config.
  * `collab-public`'s only `collab_artifacts` input is `enabled`, rendered by `herdr-eng fleet config render-collab` into the collab slice as `collab_artifacts: {enabled: <bool>}`. Its mounts and the `--collab-artifacts-mirror` / `--collab-artifacts-outbox` flags are static in `compose.collab.yaml`; it builds the bridge only when the slice says `enabled: true` **and** both flags are present (Task 9).
  * The outbox volume (staging) and the worker's spool dir must sit on large storage. `deploy.sh` creates them beside the share dirs (as the deploying uid; see Task 9), and `COLLAB.md` says so.
  * Web: everything goes through `esc()`, with no inline handlers, no iframes and no tokens in storage. Owner POSTs are same-origin. Permission denials are non-disclosing 404s.
* **Commits.** Every commit ends with the attribution trailer the executing session's system reminder prescribes.

## Review Focus

* **Revocation mid-flight.** If the owner revokes a grant while an intent sits in the outbox (or the worker is mid-tick), the next tick must reject it (`rejected`, "no write grant"), even when the agent's staging was already swept (the grant check runs before upload resolution). Within ~2 s of the revoke, `artifacts_list` must stop listing the artifact, independent of any running mirror refresh. The revoked agent's staging for that artifact must be swept. Test all three, in Tasks 5b, 5d and 9 and in Task 8c's end-to-end test.
* **Hostile names and text.** Commits containing any of the following must never escape the mirror or staging, crash the applier or let a mirror read return a file outside `blobs/`:
  * the names `../x` and `a/../../b`, a NUL or a CR in a name, and a name that is a directory prefix of another file;
  * 100 000 one-byte lines, and a very large single line.

  Pin with Task 3 (names, staging paths) and Task 5b (applier).
* **Hostile outbox tree.** `collab-public` controls the outbox and the worker runs as root. A symlinked `staging/<owner>` or `<upload_id>` component, or a symlinked intent, must never make the worker write, unlink or delete outside the outbox. Pin in Task 3 (helpers) and Tasks 5a, 5c and 5d (callers).
* **Forged or tampered staging.** None of the following may ever produce a version or reference another agent's or artifact's bytes:
  * an `upload_id` copied from another agent;
  * an upload staged for artifact X and committed to Y;
  * a part replaced on disk after staging;
  * a missing or symlinked part;
  * a declared size or sha256 that does not match.

  Each is refused with the same "not found", or with "upload failed verification". Pin in Task 3, Task 5c and Tasks 7b/7c.
* **Crash and retry.** A worker crash between `merge_commit` and the result write, or an agent resending the same `idempotency_key` with a new ticket, must yield exactly one version, and both tickets must report it. This must hold even when the restart comes more than 60 s after receipt (the `received` ledger row exempts it from the age check), and even when the staged upload was already consumed. A reused key with a **different** request is rejected, never answered with the old version. Pin in Tasks 5a, 5b and 5c.
* **Large uploads do not stall the bridge.** While a multi-GB assembly or a long mirror refresh runs, `grants.json` keeps its heartbeat and other agents' intents keep being applied. Pin in Task 9.
* **One agent cannot see another's results.** Agent B guessing or copying agent A's ticket gets the same "not found" as an unknown ticket, including during the long-poll. Pin in Task 7c.
* **Share panel tabs under failure.** If the collaborators route returns 503 (the bridge is disabled) or the agent list cannot be fetched (the collab overlay is off), the Links tab must keep working, and the Collaborators tab must say why without a broken form. Pin in Task 12.

---

### Task 1: Migration 0045 and the collaborator store

**Files:** Create `deploy/control-plane/migrations/0045_artifact_collab_bridge.sql`, `herdr_engineering/control_plane/artifact_collab_store.py`; Test `tests/control_plane/test_artifact_collab_store.py`. The test reuses the isolated Postgres fixture pattern of `test_artifact_version_parents.py`, but applies **every** migration file up to and including 0045 in filename order (not one named file).

**Interfaces:**
* Migration:
  * `artifact_collab_intents(ticket_id text PK, kind text NOT NULL DEFAULT 'commit', actor_kind text NOT NULL CHECK (actor_kind IN ('agent','user')), actor_id text NOT NULL, artifact_id text, base_version_id text, idempotency_key text, request_sha256 text, bytes bigint NOT NULL DEFAULT 0, outcome text NOT NULL CHECK (outcome ~ '^[a-z_]{1,32}$'), version_id text, attempt_id text, reason text, received_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz)`.
    * `received` means undecided. The `commit` kind's outcomes are `created`, `merged`, `conflict` and `rejected`; other kinds' handlers choose their own.
    * `request_sha256` is the applier's digest of the canonical request (Task 5a), used to refuse a reused idempotency key with a different request.
    * No FK on `artifact_id`: an intent can name an artifact that does not exist, and a non-commit kind may name none.
    * Indexes on `(artifact_id, received_at DESC)` and `(actor_kind, actor_id, received_at DESC)`.
    * `bytes` is inline text bytes only. Staged bytes are counted in `artifact_collab_uploads`.
    * Index on `(actor_kind, actor_id, kind, idempotency_key)`, for the replay short-circuit.
  * `artifact_collab_uploads(owner_key text NOT NULL, upload_id text NOT NULL, artifact_id text NOT NULL, name text NOT NULL, sha256 text NOT NULL, size_bytes bigint NOT NULL, storage_key text NOT NULL, media_type text NOT NULL, charged boolean NOT NULL, verified_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (owner_key, upload_id))`.
    * Only the worker writes it, after `put_stream` verified the bytes. It is the sole authority that an upload is usable.
    * `owner_key` is the staging owner: the agent id for collaborators (plan 5 adds `m_…` member keys).
    * `charged` is true only for uploads counted against the per-agent daily quota, which means `commit` uploads.
  * `artifact_collab_invite_grants(invite_id text PK, artifact_id text NOT NULL REFERENCES artifacts, permission text NOT NULL CHECK (permission IN ('read','write')), expires_at timestamptz NOT NULL, granted_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), resolved_agent_id text, resolved_at timestamptz)`.
  * `ALTER TABLE artifact_collab_attempts ADD COLUMN IF NOT EXISTS ticket_id text` (written by `merge_commit(ticket_id=…)`, Task 5b).
  * Include a `-- DOWN:` comment as `0043_artifact_version_parents.sql` does.
* `class CollabGrantStore(conn)`. Every method is `async`:
  * Grants:
    * `grant(artifact_id, agent_id, permission: Literal['read','write'], *, expires_at: datetime, granted_by: str) -> dict` upserts `artifact_acl` for `collab:<agent_id>`. An `expires_at` more than 90 days out raises `ValueError`.
    * `revoke(artifact_id, agent_id) -> bool`.
    * `revoke_write_grants(artifact_id) -> int` deletes every `collab:` row on the artifact whose permissions include `edit` and returns the count. It is used by the retraction fail-safe (Task 10b).
    * `revoke_all_grants(artifact_id) -> int` deletes every `collab:` row on the artifact, read and write, and returns the count. It is used only by the operator's restrict-all action (Task 10b), never automatically.
    * `live_grants() -> list[dict]` returns `{artifact_id, agent_id, permission, expires_at}` for unexpired `collab:` rows.
    * `grant_for(artifact_id, agent_id) -> dict | None`.
  * Invite grants:
    * `add_invite_grant(invite_id, artifact_id, permission, *, expires_at, granted_by)`.
    * `resolve_invite_grants(join_to_agent: dict[str, dict]) -> int` creates the real grant with `expires_at = min(row.expires_at, agent.hard_expiry)` and marks the row resolved.
    * `pending_invite_grants(artifact_id) -> list[dict]`.
  * Intent ledger:
    * `record_received(ticket_id, kind, actor: Actor, artifact_id, base_version_id, idempotency_key, request_sha256, bytes) -> dict | None` returns the existing row if the ticket is known.
    * `record_outcome(ticket_id, outcome, *, version_id=None, attempt_id=None, reason=None)`.
    * `decided_for_key(actor, kind, idempotency_key, outcomes: frozenset[str]) -> dict | None` returns the row (including `request_sha256`) whose outcome is in `outcomes`.
    * `bytes_last_24h(agent_id, *, exclude_ticket: str | None = None) -> int` is the sum over the last 24 h of two things:
      * ledger `bytes` (kind `commit`, agent actor, outcomes other than `rejected`, `ticket_id != exclude_ticket`);
      * `artifact_collab_uploads.size_bytes` where `owner_key = agent_id AND charged`.
  * Verified uploads:
    * `verified_upload(owner_key, upload_id) -> dict | None`.
    * `record_verified_upload(owner_key, upload_id, *, artifact_id, name, sha256, size_bytes, storage_key, media_type, charged: bool) -> dict` is idempotent on the primary key.
  * Activity:
    * `collaborators(artifact_id) -> list[dict]` returns grants plus `last_used_at`, `commits`, `conflicts`, `rejected` from the ledger rows of kind `commit` with agent actors.
    * `attempts(artifact_id, *, limit=100) -> list[dict]` returns ledger rows with outcome `conflict`/`rejected`, joined to `artifact_collab_attempts.conflict_names` by `ticket_id`.

- [ ] **Step 1: Write failing tests:**
  * The migration applies twice.
  * `grant`:
    * writes `["read"]` or `["read","edit"]` with principal `collab:a1`;
    * `permission='admin'` raises `ValueError`;
    * `expires_at` at 91 days raises `ValueError`.
  * `live_grants` drops expired rows.
  * `revoke` deletes the row.
  * `revoke_write_grants` deletes write rows only and leaves read rows and other artifacts untouched.
  * `revoke_all_grants` deletes read and write rows of that artifact only and leaves other artifacts untouched.
  * `record_received` twice returns the first row, and stores `request_sha256`.
  * `bytes_last_24h` sums only the last 24 h and excludes `rejected` ledger rows. A 5 GB verified upload counts once, even when two tickets reference it. `exclude_ticket=T` leaves out T's own `received` row.
  * `record_verified_upload` twice keeps one row.
  * A `charged: false` upload of 5 GB under owner key `m_x` and one under the agent's own key both leave `bytes_last_24h(agent)` unchanged.
  * `decided_for_key` ignores rows whose outcome is outside `outcomes`, and rows of another kind or actor, and returns `request_sha256`.
  * A `user` actor row with kind `x.test` and outcome `stored` is accepted by the CHECK constraint.
  * `resolve_invite_grants` caps `expires_at` at `hard_expiry` and is idempotent.
  * `collaborators` counts commits and conflicts per agent.
  * `attempts` includes conflict names, on rows seeded by hand with `ticket_id` (Task 5b makes `merge_commit` write it).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_collab_store.py -q` and expect FAIL.
- [ ] **Step 3: Implement** the migration and the store. **Step 4: Run** and expect PASS, with `tests/control_plane/test_artifact_merge_commit.py` still passing.
- [ ] **Step 5:** Re-check origin/main's migration numbers. **Commit** `feat(artifacts): collaborator grants and intent ledger (migration 0045)`.

### Task 2: Keep `collab:` principals out of the web ACL

**Files:** Modify `herdr_engineering/control_plane/artifact_acl.py`, `herdr_engineering/control_plane/artifacts.py` (`load_acl`, artifacts.py:1032), `herdr_engineering/control_plane/api.py` (the `/acl` and `/acl/revoke` routes at api.py:3712/3722); Test `tests/control_plane/test_artifact_acl.py` (extend).

**Interfaces:**
* `COLLAB_PREFIXES = ("collab:", "collab-invite:")` in `artifact_acl.py`.
* `ArtifactAcl.grant` raises `ValueError("collaborator grants use the collaborators route")` for those prefixes.
* `ArtifactAcl.load` skips them.
* `ArtifactStore.load_acl` filters `principal_id NOT LIKE 'collab:%' AND principal_id NOT LIKE 'collab-invite:%'`.
* The `/acl` routes answer 400 for those principals.

- [ ] **Step 1: Write failing tests:**
  * `ArtifactAcl().grant(a, "collab:x", {"read"})` raises.
  * `load([...collab row...])` leaves `has_grants(a)` False.
  * The legacy rule still holds: an artifact without `created_by` is still readable by a viewer after a `collab:` row exists in the DB and the ACL is reloaded.
  * POST `/api/v1/artifacts/{id}/acl` with `principal_id: "collab:x"` returns 400, and its revoke does too.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the extended test plus `tests/control_plane/test_artifacts.py` and expect PASS.
- [ ] **Step 5: Commit** `fix(artifacts): collaborator principals never enter the web ACL`.

### Task 3: Bridge file contract, limits, staging layout and safe I/O

**Files:** Create `herdr_engineering/control_plane/collab_bridge_files.py` (imported by both sides; stdlib only, no asyncpg); Modify `herdr_engineering/control_plane/share_public.py` (import `_open_beneath` from the new module instead of defining it); Test `tests/control_plane/test_collab_bridge_files.py`.

**Interfaces:**
* Protocol constants. These are not configuration; plan 5 imports them:
  * Sizes: `CHUNK_BYTES = 524_288`, `MCP_UPLOAD_MAX_BODY = 786_432`, `MAX_MESSAGE = 1000`.
  * Ages: `INTENT_MAX_AGE = 60.0`, `FEED_MAX_AGE = 120.0`.
  * Patterns: `TICKET_RE = r"ict_[0-9a-f]{24}\Z"`, `UPLOAD_RE = r"upl_[0-9a-f]{24}\Z"`, `SHA_RE = r"[0-9a-f]{64}\Z"`.
  * `MAX_CHUNK_INDEX = 9_999_999`. Part files are named `{index:07d}`.
* Limits:
  * `@dataclass(frozen=True) class BridgeLimits` holds every key of the Configuration table (including `mirror_max_manifests`), with those defaults.
  * `BridgeLimits.from_mapping(d: dict) -> BridgeLimits` refuses unknown keys, and refuses `0`, negatives and bools.
  * `to_dict()` serializes it for `grants.json`.
* Safe reads:
  * `_open_beneath` is **moved** (extracted, never copied) from `share_public.py:302` into this module, keeping its signature and `(fd, stat)` return with `max_size`; `share_public` imports it from here.
  * `read_json_beneath(root: Path, parts: list[str], *, max_bytes: int) -> dict | None`. It opens each component with `O_NOFOLLOW` through `dir_fd` (via `_open_beneath`). It accepts regular files only, enforces the size cap before parsing and returns None on any failure.
  * `read_bytes_beneath(...) -> bytes | None` is the same check for blobs and parts.
  * `open_beneath(root, parts, *, max_bytes=None) -> BinaryIO | None` is the streaming variant used by assembly. With `max_bytes` it returns a bounded reader that raises `BridgeError` once more than `max_bytes` bytes come out, even if the file grows after the open.
* Writes by the owning side (mirror for the worker, outbox for collab-public):
  * `atomic_write_json(dir: Path, name: str, obj: dict, *, mode=0o644)` serializes with `ensure_ascii=False`, writes `.tmp-<rand>`, fsyncs and `os.replace`s. It uses the share-bundle modes (files 0644, dirs 0755), so the reader container's uid can read what the other container's uid wrote. It is for trees the writer controls; the worker never uses it inside the outbox.
  * `atomic_write_bytes(dir, name, data)` does the same for bytes.
  * `link_new_bytes(dir, name, data, *, mode=0o600) -> bool`. It writes an unnamed `O_TMPFILE` in the dir fd (write, `fchmod`, fsync) and `linkat`s it to `name` via `/proc/self/fd/<fd>`, so there is never a `.tmp-*` file or an `nlink == 2` moment. Only where `O_TMPFILE` or `/proc` is unavailable does it fall back to writing `.tmp-<rand>`, linking it and removing the tmp; a stale fallback tmp is ignored by `list_parts` and removed by the sweep. It returns False if `name` already exists and never replaces it.
* Root-safe mutation inside a hostile tree (the worker's only way to touch the outbox):
  * `write_marker_beneath(root: Path, parts: list[str], name: str, data: bytes = b"", *, mode=0o644) -> bool`: opens each component of `parts` with `O_DIRECTORY|O_NOFOLLOW` relative to the previous fd (starting from `root`), then creates `name` with `O_CREAT|O_EXCL|O_NOFOLLOW|O_WRONLY` via `dir_fd`. Returns False if `name` exists; returns False (never raises) if any component is a symlink or not a directory.
  * `unlink_beneath(root, parts, name) -> bool`: fd-relative `os.unlink(name, dir_fd=…)` after the same no-follow walk; refuses a non-regular target.
  * `remove_tree_beneath(root, parts, *, max_entries=None) -> bool`: walks with `O_DIRECTORY|O_NOFOLLOW`, enumerates with `os.scandir(fd)`, `unlink`s files and symlinks (removing the link itself, never its target) and `rmdir`s subdirectories with `dir_fd`, recursing only into real directories opened no-follow. It never follows a link. It fails closed (False, tree kept) past `max_entries` entries in all (callers pass `tree_entry_cap(limits)`) or `MAX_TREE_DEPTH`.
  * `list_beneath(root, parts, *, max_entries) -> list[tuple[str, os.stat_result]] | None`: the no-follow walk, then `os.scandir(dir_fd)` and `fstatat` no-follow per entry (symlinks and FIFOs reported as such so callers skip them). None for an unsafe or symlinked component; `BridgeError("too many entries")` past `max_entries`, which callers derive from their limits (empty files cost no quota bytes). Used by Tasks 5a/5d for `intents/` and `staging/`.
* Staging:
  * `@dataclass(frozen=True) class Actor: kind: Literal['agent','user']; id: str`.
  * `agent_owner_key(agent_id) -> str` returns the agent id. It refuses an id starting with `m_`, which is reserved for plan 5's member owner keys; agent ids come from `ids.new_id("agent")` and never have that prefix.
  * `upload_id_for(owner_key, artifact_id, name, file_sha256, size_bytes) -> str`.
  * `staging_path(root, owner_key, upload_id) -> Path`, and `staging_parts(owner_key, upload_id) -> list[str]` (`["staging", owner_key, upload_id]`) for the `*_beneath` helpers. The `owner_key` comes from the authenticated principal or the validated intent, never from a caller argument.
  * `@dataclass(frozen=True) class StagedUpload: upload_id: str; owner_key: str; artifact_id: str; name: str; size_bytes: int; file_sha256: str; total: int; created_at: float`. It is persisted as `meta.json` in the upload dir.
  * `@dataclass(frozen=True) class Chunk: artifact_id: str; name: str; size_bytes: int; file_sha256: str; total: int; index: int; data: bytes`.
  * `validate_chunk(args: dict, limits: BridgeLimits) -> Chunk`. It requires:
    * strict base64;
    * the chunk's own `sha256` matches the decoded data;
    * `0 ≤ index < total`;
    * `total == ceil(size_bytes / CHUNK_BYTES)`;
    * `len(data) == CHUNK_BYTES` except for the last chunk, which is the exact remainder;
    * `1 ≤ size_bytes ≤ limits.max_file_bytes`;
    * the name passes `normalise_name`/`check_names`.
  * `upload_complete(root, owner_key, upload_id) -> bool` holds when the indices `0..total-1` are all present, or when the worker's `verified` marker is present. The marker is a hint for collab-public only; the worker never trusts it.
  * `list_parts(root, owner_key, upload_id, *, max_parts)` returns the present indices below `max_parts` (the validated `meta.total`, or `max_parts_for(limits)`), stat'ing only part-shaped names and failing closed with `BridgeError` past `max_parts + ENTRY_SLACK` entries.
* Intent kinds:
  * `register_intent_kind(kind: str, validate_payload: Callable[[dict, BridgeLimits], object], *, actors: frozenset[str]) -> None`. `kind` must match `^[a-z][a-z0-9_.]{0,47}\Z`. Registering a kind twice raises. `actors` is the set of actor kinds allowed to send it.
  * Plan 3 registers only `commit` (`validate_commit_payload`, `actors={"agent"}`), at import of `collab_bridge_files`. Plan 4 (`annotation_resolve`) and plan 5 (`member.*`) register theirs from their own modules.
* Validation:
  * `new_ticket() -> str`.
  * `validate_intent(raw: dict, *, filename: str, limits: BridgeLimits) -> Intent` raises `IntentError(reason)`.
    * The envelope is `{ticket, kind, actor, artifact_id?, idempotency_key?, submitted_at, payload}`.
    * An unregistered kind gives `IntentError("unknown intent kind")`, and so does an actor kind not allowed for the kind.
  * `request_digest(raw: dict) -> str`: sha256 of the canonical JSON (`sort_keys=True, separators=(",", ":"), ensure_ascii=False`) of `{kind, actor, artifact_id, payload}` from the envelope (ticket and `submitted_at` excluded), so the same request resent with a new ticket has the same digest.
  * `@dataclass(frozen=True) class Intent: ticket: str; kind: str; actor: Actor; artifact_id: str | None; idempotency_key: str | None; submitted_at: float; payload: object; request_sha256: str`.
  * `@dataclass(frozen=True) class CommitPayload: base_version_id: str; message: str; changes: tuple[Change, ...]; inline_bytes: int` is what `validate_commit_payload` returns. `artifact_id` is required for `commit`.
  * `@dataclass(frozen=True) class Change: name: str; op: Literal['put','delete']; text: str | None; upload_id: str | None`.
  * `validate_changes(changes: list, limits: BridgeLimits) -> tuple[Change, ...]` is shared by `collab-public` and the worker. It:
    * runs `normalise_name` and `check_names` from `share_bundle`;
    * requires exactly one of `text`/`upload_id` for a put, and neither for a delete;
    * matches `upload_id` against `UPLOAD_RE`;
    * rejects keys outside `{name, op, text, upload_id}`, so `sha256`, `storage_key`, `entry`, `size_bytes` and `media_type` are refused;
    * enforces `max_changes_per_commit`;
    * refuses C0 control characters other than `\t`, `\n` and `\r` in inline text.

- [ ] **Step 1: Write failing tests:**
  * **Safe reads:**
    * A symlinked intent file, a symlinked parent dir, a FIFO and an oversize file all return None.
    * A symlinked part makes `open_beneath` return None.
  * **Writes:**
    * `atomic_write_json` never leaves a partial target. Simulate this by raising inside write: the target is unchanged and the tmp file is removed.
    * `link_new_bytes` on an existing name returns False and leaves the original bytes.
  * **Root-safe mutation (hostile tree):** with `outside/` a sibling of the outbox root holding a sentinel file,
    * `staging/<owner>` as a symlink to `outside/`: `write_marker_beneath(root, ["staging", owner, upl], "verified")` returns False and creates nothing in `outside/`; `remove_tree_beneath(root, ["staging", owner, upl])` returns False and the sentinel survives.
    * `staging/<owner>/<upload_id>` as a symlink to `outside/`: the same two calls leave `outside/` untouched.
    * A real upload dir containing a symlink `0000001 -> outside/sentinel`: `remove_tree_beneath` removes the link and the dir, and the sentinel survives.
    * `unlink_beneath` on a symlinked intent removes nothing outside and refuses the non-regular target.
    * `write_marker_beneath` on an existing marker returns False.
  * **Intents and changes:**
    * A filename and ticket mismatch raises, as do `kind: "share_create"` and an unknown top-level key.
    * A `commit` from a `user` actor raises.
    * A test-registered kind `x.test` (with `actors={"user"}`) validates through its own payload validator, and a second registration of it raises. The test registry is reset afterwards.
    * `request_digest` is equal for two envelopes that differ only in ticket and `submitted_at`, and differs when a change differs.
    * `agent_owner_key("m_abc")` raises.
    * Each of these change names raises: `../x`, `/abs`, a NUL, a CR, `a` together with `a/b`, and duplicates.
    * A change carrying `storage_key` raises, as does one carrying `sha256`, and one carrying `size_bytes`.
    * A put with both `text` and `upload_id` raises, as does one with neither. An `upload_id` of `upl_../x` raises.
    * `max_changes_per_commit + 1` changes raise.
    * Text containing `\x01` raises; `\t`, `\n` and `\r` are accepted. A non-str text raises.
  * **Chunks:**
    * A chunk whose sha256 mismatches raises.
    * Bad base64 raises.
    * A non-last chunk of `CHUNK_BYTES - 1` raises, and so does a last chunk with a wrong remainder.
    * `index == total` raises, a `total` inconsistent with `size_bytes` raises, and `size_bytes` over `max_file_bytes` raises.
    * `upload_id_for` is deterministic and differs per owner key.
  * **Limits:** `BridgeLimits.from_mapping({"daily_bytes_per_agent": 0})` raises, as does an unknown key and a bool value. The defaults equal the Configuration table (including `mirror_max_manifests == 100000`).
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS, along with `tests/control_plane/test_share_public.py` (the extracted `_open_beneath`).
- [ ] **Step 5: Commit** `feat(collab): artifact bridge file contract and chunk staging layout`.

### Task 4: Mirror publisher (worker side)

**Files:** Create `herdr_engineering/control_plane/collab_mirror.py`; Test `tests/control_plane/test_collab_mirror.py` (Postgres fixture and a fake RustFS keyed by sha256 that implements `iter_get`).

**Interfaces:**
* Consumes `CollabGrantStore.live_grants` (Task 1), `ArtifactStore.list_versions`/`get_version`, `ArtifactGraphGate.load_retractions`, `RustFSClient.iter_get(key, sha)` (rustfs.py:115) and `BridgeLimits`/`atomic_write_*` (Task 3).
* Produces `class CollabMirror(root: Path, *, rustfs, limits: BridgeLimits, clock=time.time)`. History depth, manifest count, per-file and per-artifact caps come from `limits.mirror_history_versions`, `mirror_max_manifests`, `mirror_max_file_bytes` and `mirror_max_artifact_bytes`. There is no hard-coded manifest cap. Its methods:
  * `async publish_grants(grants, store, *, heartbeat: float = 30.0) -> bool`:
    * It writes `grants.json = {generated_at, limits: limits.to_dict(), agents: {agent_id: {artifact_id: {permission, expires_at}}}}`.
    * It writes at once when the content changed, and otherwise at least every `heartbeat` seconds, refreshing `generated_at` so the 120 s freshness check in collab-public never trips while the worker is alive.
    * It returns whether it wrote. It never takes an artifact lock and never waits on a refresh.
  * Concurrency: `CollabMirror` keeps one `asyncio.Lock` per artifact id; `refresh` and `remove` of the same artifact hold it, so the applier (several lanes) and the mirror task never refresh one artifact concurrently.
  * `async refresh(store, artifact_id, *, gate) -> dict`, under the artifact's lock. It writes `artifacts/<artifact_id>/blobs/<sha256>` for the text files (UTF-8 decodable, ≤ `mirror_max_file_bytes`) of the head and the previous `mirror_history_versions` non-retracted versions. It streams each blob with `rustfs.iter_get` to a temp file and renames it into place, skips blobs that exist, and stops adding blobs once the artifact reaches `mirror_max_artifact_bytes`; the files left out get `mirrored: false`. It then writes `head.json` **last**:
    * `{artifact_id, title, artifact_type, writable: bool, writable_reason, head: {version_id, version_number}, versions: [{version_id, version_number, parent_version_ids, created_via, collaborator_agent_id, author_label, message, created_at, merge: bool, mirrored: bool, files: [{name, size_bytes, sha256, media_type, text: bool, mirrored: bool}]}]}`, newest first and capped at `mirror_max_manifests`.
    * `author_label` is `"owner"` or `"collaborator <agent_id>"` and never an email. `message` is `provenance.message`, capped at 1000 characters.
    * After the swap, still under the lock, it deletes only files in `blobs/` that the `head.json` it just wrote does not reference. It never deletes any other file in the artifact dir (plan 4 writes `annotations.json` there).
  * `async remove(artifact_id)` deletes the artifact's whole mirror dir, under the artifact's lock.
  * `async sync(store, *, gate, force: set[str] = frozenset())` refreshes every granted artifact whose head changed (or that is in `force`), and removes artifacts that have no live grant or are retracted. It does **not** publish grants; the loop's grants task does (Task 9).

- [ ] **Step 1: Write failing tests:**
  * **Content:**
    * A granted plain artifact gets `head.json` and blobs whose sha256 matches the content.
    * A binary or non-UTF-8 file is listed with `text: false` and has no blob.
    * A file over `mirror_max_file_bytes` (set small in the test) is listed with `mirrored: false` and gets no blob.
    * With `mirror_history_versions: 5`, the 7th-newest version has `mirrored: false` and no blobs.
    * With `mirror_max_manifests: 3` (set small), `versions` holds the newest 3; with the default, all versions are listed.
    * A report artifact has `writable: false, writable_reason: "report artifacts are read-only for collaborators"`.
  * **Retraction and revocation:**
    * A retracted version is absent from `versions`, and a retracted artifact's directory is removed.
    * Revoking the last grant removes the directory on the next `sync`.
  * **`grants.json`:**
    * It carries `limits` equal to the configured `BridgeLimits`.
    * It is rewritten immediately on a grant change. When unchanged, it is not rewritten within 30 s but is rewritten (newer `generated_at`) after 30 s (injected clock).
  * **Concurrency:**
    * Two concurrent `refresh` calls of one artifact (a fake `iter_get` that yields slowly, and a new head version committed between them) both finish; every blob the final `head.json` references exists.
    * An `annotations.json` placed in the artifact dir survives `refresh`, and `remove` deletes it with the rest.
  * **Safety:**
    * A crash simulated mid-refresh leaves the previous `head.json` intact.
    * No file in the mirror contains a `created_by` email: assert against a seeded `owner@example.org`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): read-only collaborator mirror`.

### Intent applier (Tasks 5a–5d): shared design

Tasks 5a–5d build `herdr_engineering/control_plane/collab_intents.py` in four commits. This section is the design all four share; each task names the part it implements and its tests. Tests live in `tests/control_plane/test_collab_intents.py` (Postgres fixture, fake RustFS, temp outbox and mirror).

* Consumes:
  * Task 1's store;
  * Task 3's `validate_intent`, `request_digest`, `read_json_beneath`, `open_beneath`, `write_marker_beneath`, `unlink_beneath`, `remove_tree_beneath`, `StagedUpload`, `staging_parts`, `upload_complete` and `BridgeLimits`;
  * Task 4's `CollabMirror.refresh`;
  * `ArtifactStore.merge_commit`/`register_report_blob`/`get_version`/`load_retractions`;
  * `RustFSClient.put_stream`.
* `class CollabIntentApplier(outbox: Path, mirror: CollabMirror, *, rustfs, limits: BridgeLimits, spool_dir: Path, lineage: Callable[[dict], dict] | None = None, context_extras: Mapping[str, object] = {}, clock=time.time, max_per_tick: int = 20)`.
  * `register(kind, handler, *, lane_key=None, replay_outcomes: frozenset[str] = frozenset())`. Plan 3 registers `commit` with `replay_outcomes={"created","merged"}` (Task 5b).
    * `handler` is `async (ctx: IntentContext, intent: Intent) -> dict`. It returns `{result, version_id?, reason?, …}`, and the applier records and writes that.
    * `lane_key` is `(intent, agent_row | None) -> str`. The default is `f"{actor.kind}:{actor.id}"`. Plan 5 maps every actor of one member to that member's lane.
  * `@dataclass class IntentContext: actor: Actor; agent: dict | None; conn; store: ArtifactStore; grants: CollabGrantStore; limits: BridgeLimits; assemble; extras: Mapping[str, object]`.
    * `agent` is the actor's `agents.json` row, or None for user actors.
    * `assemble(owner_key, upload_id, *, artifact_id, name, charge: bool) -> entry` is the staged-upload path of commit step 10 as a callable (Task 5c): reuse a verified upload, else assemble, verify, `register_report_blob` and `record_verified_upload(charged=charge)`.
    * `extras` is `context_extras`, passed through untouched. Plan 5 supplies `share_store`/`server` there, so plan 3 never imports them.
  * `async tick(acquire) -> dict` returns `{applied, conflicts, rejected, skipped}`.
    * `acquire` is an async context manager that yields a connection from the applier's own pool.
    * It runs up to `limits.apply_concurrency` intents concurrently, and never two with the same lane key at once. Within a lane, intents run oldest first.
    * A lane that is still busy from the previous tick is skipped, not awaited.
  * `register_staging_owner(prefix: str, keep: Callable[[str, StagedUpload], Awaitable[bool]])` and `async sweep_staging(conn, *, agents: dict, grants: list[dict]) -> int` (Task 5d).
  * `sweep_results(max_age=3600)` (Task 5a): deletes result files in the mirror older than `max_age`.
* Per intent file (names matching `TICKET_RE + ".json"`, regular files only, read with `read_json_beneath`). The steps run in this order.
  * **Generic, at scan time, before lane wait (Task 5a)** — run for every kind:
    1. If the ledger has the ticket with a decided outcome, rewrite the result file from the row and unlink the intent.
    2. Validate the intent; an invalid one, or an unregistered kind, gets `rejected` with a fixed reason and no echo of content.
    3. If `decided_for_key(actor, kind, intent.idempotency_key or ticket, replay_outcomes)` finds a row: when its `request_sha256` equals `intent.request_sha256`, record this ticket with the same outcome and version, write its result and unlink it; otherwise record this ticket `rejected` "idempotency key was reused for a different request". This happens before any upload lookup, so a retry after the upload was consumed still succeeds.
    4. If there is no ledger row and `now - submitted_at > INTENT_MAX_AGE`, reject `expired`. Otherwise `record_received(…, request_sha256=intent.request_sha256, bytes=inline_bytes)`, where `inline_bytes` is 0 for kinds other than `commit`. From here on the age clock no longer applies, so an intent waiting behind its own lane's multi-GB assembly never expires.
    5. For an agent actor, `agents.json` (`read_json_beneath`, ≤ 4 MiB) must be fresh and must show the agent `active` with `expires_at > now`. A user actor skips this step; its kind's handler authenticates it (plan 5: against its member directory). The applier then computes the lane and dispatches to the kind's handler when the lane is free.
  * **`commit` handler, in the lane** — the grant is checked before anything touches staging, so a revoked agent whose staging was swept gets "no write grant", never "upload not found":
    6. `grant_for` must show `write`, unexpired (Task 5b).
    7. Check that the artifact is writable, and the gate plus version `state` for the base and the current head (Task 5b).
    8. Resolve each staged put (Task 5c):
       * A `verified_upload(agent, upload_id)` row with the same `artifact_id` and `name` is reused as is, with no staging read and no quota charge.
       * Otherwise read `meta.json` with `read_json_beneath(outbox, staging_parts(agent_owner_key(intent.actor.id), upload_id) + ["meta.json"])`. Missing gives `rejected` "upload not found", and so does a different `artifact_id` or `name`.
       * This path is derived from the intent's agent, so another agent's `upload_id` is always "not found".
    9. Apply the daily quota: `bytes_last_24h(agent, exclude_ticket=ticket) + inline_bytes + Σ declared size_bytes of unverified uploads ≤ daily_bytes_per_agent`. This check runs before any assembly or RustFS write (inline part Task 5b, staged part Task 5c).
    10. Store the content:
       * An inline put's text is stored with `rustfs.put` and `register_report_blob(artifact_id, …)` (Task 5b).
       * A staged put goes through `ctx.assemble(agent_owner_key(agent), upload_id, artifact_id=…, name=…, charge=True)` (Task 5c). An unverified staged put requires all its parts, else `rejected` "upload incomplete".
         * Its parts are streamed in index order through `open_beneath` (bounded memory) into `asyncio.to_thread(rustfs.put_stream, reader, expected_sha256=meta.file_sha256, expected_size=meta.size_bytes, media_type=…, spool_dir=spool_dir)`.
         * Then come `register_report_blob` and `record_verified_upload`.
         * The worker then writes the `verified` marker with `write_marker_beneath` and deletes the parts with `unlink_beneath`, leaving only `meta.json`, `audited` (if present) and `verified` until the TTL, so collab-public's precheck still passes for a re-commit.
         * An `IntegrityError` gives `rejected` "upload failed verification", and the whole upload dir is deleted with `remove_tree_beneath`.
       * The entry is `{name, media_type, sha256, size_bytes, storage_key}`. `media_type` is the head entry's media type, else `mimetypes.guess_type`, else `"text/plain"` for inline text or `"application/octet-stream"` for staged bytes.
    11. Call `merge_commit(created_by=f"collab:{agent}", collaborator_agent_id=agent, idempotency_key=intent.idempotency_key or ticket, ticket_id=ticket, max_text_bytes=limits.max_text_merge_bytes, …)` with no outer transaction. Large and binary puts need no special case: a change on one side only is taken whole, and a change on both sides conflicts as `binary`/`oversize`. (Task 5b)
    12. Finish the intent (Task 5b; the generic applier does the record/write/unlink for every kind from the handler's return value):
        * `record_outcome` (with `attempt_id` from `ArtifactMergeConflict.attempt_id` on a conflict).
        * Write `results/<ticket>.json = {ticket, kind, actor: {kind, id}, artifact_id, result, version_id?, version_number?, head_version_id?, merged_names?, conflicts?: [{name, kind, merged_text, truncated}], reason?, decided_at}`, applying the `max_conflict_text_bytes` cap.
        * Unlink the intent with `unlink_beneath(outbox, ["intents"], name)`.
        * On success, call `mirror.refresh` and `lineage(version)` (the version dict from `merge_commit`, which carries `artifact_id` and `version_id`; the poster is built in Task 6 and wired in Task 9). Their errors are logged and never fail the tick.
* Every worker write or delete inside the outbox uses only `write_marker_beneath`, `unlink_beneath` and `remove_tree_beneath`: the `verified` marker, part deletion, the whole-dir delete on `IntegrityError`, `sweep_staging` and the intent unlink. Results are written into the mirror (worker-owned) with `atomic_write_json`.
* Log lines carry only ticket, upload id, agent, artifact and outcome.

### Task 5a: Applier core — registry, lanes, ledger replay, generic steps

**Files:** Create `herdr_engineering/control_plane/collab_intents.py` (the class, `register`, `IntentContext`, `tick` with lanes, generic steps 1–5, the result writer, `sweep_results`); Test `tests/control_plane/test_collab_intents.py`.

**Interfaces:** as in "Intent applier: shared design" for the constructor, `register`, `IntentContext`, `tick`, `sweep_results` and steps 1–5. Until Task 5c, `IntentContext.assemble` is a callable that raises `NotImplementedError`; no Task 5a test uses it. Tests drive the core with test-registered kinds (`x.test`), so the `commit` handler is not needed yet.

- [ ] **Step 1: Write failing tests:**
  * **Registry and actors:**
    * A test kind `x.test` (`actors={"user"}`) sent by a user actor reaches its handler with `ctx.agent is None` and `ctx.extras` as passed, and its result file carries `actor: {kind: "user", …}`.
    * The same kind's replay by `idempotency_key` uses its own `replay_outcomes`.
    * An unregistered kind is `rejected`.
    * A user actor sending `commit` is `rejected` (validation; `commit` is registered at import of `collab_bridge_files`).
  * **Lanes:**
    * Two intents from one agent are applied in order and never concurrently, while an intent from a second agent is applied in the same tick.
    * A test kind registered with a `lane_key` that maps two different actors to one lane runs their intents serially.
    * A lane still busy from the previous tick is skipped, and another lane's intent is applied in that tick.
  * **Age and feed:**
    * An intent older than 60 s with no ledger row is `expired`.
    * An agent intent queued behind a slow handler of the same lane for longer than 60 s, after its `received` row was recorded at scan time, is applied, not expired.
    * An agent missing from `agents.json` or revoked there is `rejected`; a stale `agents.json` (> 120 s) is `rejected` with "agent feed stale".
  * **Crash and retry (with `x.test`, agent actor):**
    * An intent with a `received` row older than 60 s is still applied (crash recovery).
    * Re-running the same file after a simulated crash before unlink yields the same result without a second handler call.
    * Two tickets with the same `idempotency_key` and the same request call the handler once, and both results carry its outcome.
    * A second ticket with the same `idempotency_key` but a different payload is `rejected` "idempotency key was reused for a different request", and the first result is unchanged.
  * **Forged input and hostile tree:**
    * `kind: "share_create"` is `rejected`, no share row exists and the share module is never imported (assert `"herdr_engineering.control_plane.artifact_share_api" not in sys.modules` within a fresh subprocess run of the tick). The subprocess imports only plan 3's modules and registrations, which is the "members disabled" configuration. Plan 5's share handlers must import that module lazily, inside the handler, so this test keeps holding.
    * A symlinked intent file is skipped and never followed; the link's target outside the outbox is not unlinked.
    * `intents/` replaced by a symlink to an outside dir: nothing outside is read or unlinked.
  * **Results:** `sweep_results` deletes result files older than `max_age` and keeps newer ones.
  * **No secrets in logs:** an invalid intent carrying `SECRET-SENTINEL-…` text is rejected with a fixed reason; neither `caplog` nor its result file contains the sentinel.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): collaborator intent applier core with lanes and ledger replay`.

### Task 5b: The `commit` handler with inline text, merge, results, mirror and lineage hooks

**Files:** Modify `herdr_engineering/control_plane/collab_intents.py` (register the `commit` handler: steps 6, 7, 9 for inline bytes, 10 for inline text, 11 and 12); Modify `herdr_engineering/control_plane/artifacts.py`:
* `merge_commit(..., ticket_id: str | None = None)` writes `ticket_id` into its `artifact_collab_attempts` INSERT (artifacts.py:357; the row id is still `str(uuid.uuid4())`);
* `ArtifactMergeConflict.__init__(..., *, attempt_id: str | None = None)` (artifacts.py:43) stores `attempt_id`, and `merge_commit` passes the attempt row's id when it raises. The keyword default keeps every existing raise valid.

Test `tests/control_plane/test_collab_intents.py` (extend) and one new case in `tests/control_plane/test_artifact_merge_commit.py`.

**Interfaces:** as in "Intent applier: shared design" for steps 6, 7, 9 (inline part), 10 (inline part), 11 and 12. The handler records `attempt_id` from the conflict on the ledger row.

- [ ] **Step 1: Write failing tests:**
  * **`merge_commit` (in `test_artifact_merge_commit.py`):** with `ticket_id="ict_…"` a conflict writes that `ticket_id` on the attempt row and the raised `ArtifactMergeConflict.attempt_id` equals the row id; without `ticket_id` the column is NULL and existing tests still pass.
  * **Outcomes:**
    * A fast-forward gives `created` with parents `[head]`.
    * A stale base with disjoint edits gives `merged` with parents `[head, base]`.
    * Overlapping edits give `conflict` with markers in `merged_text`, an unchanged head, one `artifact_collab_attempts` row with `ticket_id` equal to the ticket, and the ledger row `conflict` with that `attempt_id`. This proves the attempt row persisted (no outer transaction). `CollabGrantStore.attempts` then lists the conflict names.
    * Conflict text over `max_conflict_text_bytes` (set small) gives `merged_text: null, truncated: true`.
  * **Refusals:**
    * No grant is `rejected` with "no write grant", and so is a read-only grant.
    * A grant revoked after the intent was written is `rejected` "no write grant", even when the intent references an `upload_id` whose staging dir no longer exists.
    * An expired grant is `rejected`.
  * **Retraction and scope:**
    * An artifact retracted in the DB is `rejected`.
    * A retracted head version (`state`) is `rejected`.
    * A gate-only retraction loaded from `load_retractions` is `rejected`.
    * A report artifact is `rejected` with "not writable by collaboration".
  * **Hostile names and text:** the names `../x`, `a/../../b`, a NUL, a CR, and a directory prefix of an existing file are `rejected` without crashing the tick; 100 000 one-byte lines and one very large single line (under the body cap) commit or conflict normally.
  * **Quota (inline):** inline bytes that push the agent over `daily_bytes_per_agent` (set small) are `rejected` "daily quota exceeded"; a crash-retry of the same ticket is not counted twice (its own `received` row is excluded).
  * **Forged input:** a forged change with `storage_key` pointing at another artifact's scoped blob is `rejected` by validation, and that blob is never referenced.
  * **Crash and retry with real commits:** two tickets with the same `idempotency_key` produce one version, and both results name it.
  * **Hooks:** on `created`/`merged`, `mirror.refresh` is called for the artifact and the injected fake `lineage` receives a dict with `artifact_id` and `version_id`; a raising `lineage` or `refresh` is logged and the tick still reports the outcome.
  * **No secrets in logs:** the committed text `SECRET-SENTINEL-…` never appears in `caplog`. The result file of a `rejected` intent does not contain the text.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_merge_commit.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): apply collaborator commit intents in the worker`.

### Task 5c: Staged-upload assembly, `rustfs.put_stream(spool_dir)` and the full quota

**Files:** Modify `herdr_engineering/control_plane/collab_intents.py` (step 8, `IntentContext.assemble`, the staged part of steps 9 and 10); Modify `herdr_engineering/control_plane/rustfs.py` (an optional `spool_dir` keyword on `put_stream`, passed to `NamedTemporaryFile(dir=…)`; the default is unchanged); Test `tests/control_plane/test_collab_intents.py` (extend), plus a `spool_dir` case in `tests/control_plane/test_rustfs_streaming.py`.

**Interfaces:** as in "Intent applier: shared design" for steps 8, 9 (staged part), 10 (staged part) and `IntentContext.assemble`.

- [ ] **Step 1: Write failing tests:**
  * **RustFS:** `put_stream(..., spool_dir=d)` creates its temp file in `d`; without it the default temp dir is used.
  * **Staged puts:**
    * A 3-chunk staged binary file (non-UTF-8, named `blob.bin` so the media type is `application/octet-stream`) is assembled. It gives `created` with an entry whose sha256 and size equal the declared ones, and the parts are gone.
    * A staged text file over `max_text_merge_bytes` (set small) changed on one side only gives `merged`.
    * The same file changed on both sides gives `conflict` with kind `oversize` and `merged_text: null`.
    * A binary file (non-text name and media type; a non-UTF-8 `*.txt` would give `not_utf8`, not `binary`) changed on both sides gives `conflict` with kind `binary`.
    * Re-committing that upload with `base = head_version_id` gives `created`, with no second `put_stream` call and no second quota charge.
    * After verification, the upload dir holds only `meta.json` and the `verified` marker (plus `audited` if collab-public wrote it).
  * **Staging refusals:**
    * Each of these is `rejected` "upload not found", and in every case nothing is put to RustFS:
      * an `upload_id` that exists only under another agent's dir;
      * an upload whose `meta.artifact_id` is another artifact;
      * an upload whose `meta.name` differs from the change's name.
    * A missing part is `rejected` "upload incomplete".
    * A part whose bytes were overwritten on disk after staging is `rejected` "upload failed verification": no version, no `artifact_blobs` row and no `artifact_collab_uploads` row exist, and the upload is deleted.
    * A forged `verified` marker with no table row is ignored: the parts are assembled or the intent is rejected as incomplete.
    * A symlinked part is `rejected`.
    * `staging/<agent>` or `staging/<agent>/<upload_id>` replaced by a symlink to an outside dir after staging: the intent is `rejected`, no marker is written outside and nothing outside is deleted.
  * **Quota:** a declared size that pushes the agent over `daily_bytes_per_agent` is `rejected` "daily quota exceeded" before any RustFS call: assert that the fake's put counters are 0.
  * **Crash and retry:** re-running the same file after a simulated crash before unlink yields the same version, even though the staged parts were already deleted; a second ticket with the same key for a consumed staged put short-circuits on the key.
  * **Owner keys:** `ctx.assemble(..., charge=False)` under owner key `m_test` verifies and stores the upload and records it uncharged. The agent's `bytes_last_24h` is unchanged.
  * **No secrets in logs:** the staged bytes never appear in `caplog`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_rustfs.py` and `test_rustfs_streaming.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): assemble and verify staged collaborator uploads`.

### Task 5d: `sweep_staging` and the staging-owner registry

**Files:** Modify `herdr_engineering/control_plane/collab_intents.py`; Test `tests/control_plane/test_collab_intents.py` (extend).

**Interfaces:**
* `register_staging_owner(prefix: str, keep: Callable[[str, StagedUpload], Awaitable[bool]])`. Owner dirs whose key starts with `prefix` are exempt from the agent and grant rules of `sweep_staging`. They are deleted only past the TTL or when `keep` returns False. Plan 3 registers no prefix.
* `async sweep_staging(conn, *, agents: dict, grants: list[dict]) -> int` deletes upload dirs (always with `remove_tree_beneath`) in these cases:
  * the dir is older than `staging_ttl_seconds` by its newest part mtime;
  * the agent is not `active` in `agents.json`;
  * the agent has no live write grant on the upload's `artifact_id`;
  * a `meta.json` is missing or invalid and the dir is older than 60 s. The grace age avoids racing collab-public between creating the dir and linking `meta.json`.

  The agent and grant rules apply only to agent owner dirs. A dir under a registered prefix follows its `keep` predicate instead. It enumerates `staging/` with no-follow opens and skips (never follows) a symlinked owner or upload entry. It never deletes an upload referenced by an intent file still in the outbox, and it returns the count.

- [ ] **Step 1: Write failing tests:**
  * It removes an upload older than the TTL, an upload of a revoked agent and an upload whose write grant is gone.
  * It keeps an upload referenced by a pending intent, and a dir younger than 60 s without `meta.json`.
  * With a test prefix `m_` registered and `keep` returning True, an `m_x` dir with no grant survives. It is removed past the TTL or once `keep` returns False.
  * A symlinked `staging/<agent>` and a symlinked `staging/<agent>/<upload_id>`, both pointing at an outside dir with a sentinel, are not followed: the sentinel survives.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): sweep collaborator staging`.

### Task 6: Collaborator lineage route and poster

**Files:** Modify `herdr_engineering/control_plane/api.py` (add `_artifact_collab_lineage` beside `_artifact_job_lineage`, api.py:5233), `herdr_engineering/control_plane/artifact_lineage.py` (factor the section validation out of `lineage_poster_from_section` into one helper, and add `collab_lineage_poster_from_section`); Test `tests/control_plane/test_artifact_collab_lineage.py`.

**Interfaces:**
* `POST /api/v1/artifacts/<id>/versions/<vid>/collab-lineage` with body `{}` (any key gives 400). It needs `edit` on the artifact.
  * It reads the version from the registry, and answers 409 unless `created_via == "collaborator"`.
  * It answers 409 when the web's in-memory `artifact_graph_gate.version_retracted` holds. This is the web-side gate check.
  * It emits with `emit_version_lineage(…, scope=visibility_fields(gated=True))`, which already emits one supersession edge per parent.
* `_poster_settings(section, *, where: str) -> tuple[str, str, float] | None` in `artifact_lineage.py`: the existing checks of `lineage_poster_from_section` (allowed keys `_POSTER_KEYS`, http(s) `api_url`, `key_file`, `timeout_seconds` in (0, 120], non-empty key read from the file) returning `(url, key, timeout)`. `lineage_poster_from_section` is refactored to use it with no behaviour change; there is one copy of the validation.
* `collab_lineage_poster_from_section(section) -> Callable[[dict], dict] | None` uses `_poster_settings` on `control_plane.artifact_worker.lineage` (`api_url`, `key_file`, `timeout_seconds`). Its hook takes the version dict `merge_commit` returns and POSTs `{}` to `/api/v1/artifacts/<artifact_id>/versions/<version_id>/collab-lineage` (both quoted); a dict without `artifact_id`/`version_id` returns `{"skipped": …}`. Task 9 wires it into `CollabIntentApplier(lineage=…)`.

- [ ] **Step 1: Write failing tests:**
  * An owner version gives 409; a collaborator merge version emits two supersession edges.
  * A retracted version (gate) gives 409.
  * An unknown version gives 404, and so does a caller without `edit`.
  * The poster builds the exact URL and headers, and its body is `{}`.
  * Both posters refuse the same bad sections with the same messages (shared helper).
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `test_artifact_lineage.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): lineage for collaborator versions`.

### collab-public bridge (Tasks 7a–7c): shared design

Tasks 7a–7c build `herdr_engineering/control_plane/collab_artifacts.py` in three commits, tested in `tests/control_plane/test_collab_artifacts.py` with a real `CollabService` and two agents (A granted write on X, B granted read on Y), a hand-written mirror (including `limits`) and a temp outbox.

* `class CollabArtifactBridge(service: CollabService, mirror: Path, outbox: Path, *, clock=time.time)`. Each method takes an authenticated agent `Principal` and raises `CollabError`.
* Denials:
  * A missing grant, an ungranted artifact and an unknown artifact all give the same 404 "not found".
  * Denials go through `service._audit_denied(pr, "artifact.<tool>", reason, resource_id=artifact_id)` (collab.py:1296), which commits `audit_denied`.
  * Allowed reads are audited `artifact_tool_used` at most once per 600 s per (agent, artifact, tool). A bounded in-memory counter per key counts suppressed calls; an event is emitted on the first call and on the first call after the window has passed, and carries `calls` = the calls since the previous event, including this one.

### Task 7a: Bridge reads, list/head/history, the `agents.json` feed and audit

**Files:** Create `herdr_engineering/control_plane/collab_artifacts.py`; Modify `herdr_engineering/control_plane/collab.py`:
* add the event types `artifact_tool_used`, `artifact_commit_submitted` and `artifact_upload_staged` to `BULK_EVENTS`, to `_log_limit` (collab.py:591) and to the `workspace_terminal_authorized` early return in `_touch_rooms` (collab.py:650). `_apply` needs no change (its if/elif chain ignores unknown types);
* add an `artifact_bridge` attribute, None by default.

Test `tests/control_plane/test_collab_artifacts.py`.

**Interfaces:**
* `available() -> bool`: `grants.json` exists, is fresh (≤ `FEED_MAX_AGE`) and carries a valid `limits`. Task 8a uses it to tell "bridge unavailable" from "no grant".
* `limits() -> BridgeLimits` comes from the fresh `grants.json` (`BridgeLimits.from_mapping`). A missing or invalid value raises 503 "collaboration bridge unavailable".
* `has_grants(pr) -> bool` reads `grants.json`, which must be fresh, and checks the grant is unexpired. `has_write_grant(pr) -> bool` is the same check for a `write` grant on at least one artifact (Task 8b uses it). Both are False when not `available()`.
* `list(pr) -> dict` returns `{artifacts: [{artifact_id, title, permission, writable, head_version_id, head_version_number}]}`.
* `head(pr, artifact_id) -> dict`.
* `read(pr, artifact_id, name, *, version_id=None, offset=0, max_bytes=262144) -> {name, version_id, size_bytes, offset, text, next_offset}`.
  * `max_bytes` must be ≤ 1 MiB, and slices end on a UTF-8 boundary.
  * It verifies the blob's sha256 against the manifest, caching a successful verification per `(path, st_ino, st_size, st_mtime_ns)` in a bounded in-memory LRU (e.g. 4096 entries), so repeated slices of a large blob are not re-hashed; any change of those stat fields re-verifies.
  * An unmirrored version or file gives 404 "version not mirrored", and a binary file gives 415 "binary files are not readable through collaboration".
* `history(pr, artifact_id, *, limit=50) -> dict`.
* `write_agents_feed() -> None` atomically writes `agents.json = {generated_at, agents: {agent_id: {owner, status, expires_at, external: bool, join_id, hard_expiry}}}`, with `join_id` from the reverse of `_joins_by_id`.
  * `_housekeeping` is request-driven, so it cannot be the trigger. A daemon thread started with the bridge writes the feed every 15 s.
  * It also runs on any agent revoke or disable (and, from Task 7c, inside `append_intent`).
  * Group membership is not in the feed (deferred to plan 5's `member-directory.json`; see Spec deviations).

- [ ] **Step 1: Write failing tests:**
  * **Listing and reads:**
    * A lists only X and B only Y.
    * A reading Y gets 404, the same as an unknown artifact, and the denial is audited as `audit_denied`.
    * `grants.json` older than 120 s, or without `limits`, makes `available()` and `has_grants` False and every tool method 503 "collaboration bridge unavailable".
    * A `read` slice never splits a multibyte character.
    * A blob whose sha256 differs from the manifest gives 502 "mirror integrity" and returns no text.
    * Two slices of one unchanged blob hash it once (count calls to the hasher); replacing the blob file (new inode/mtime) makes the next slice re-verify and, if tampered, give 502.
  * **Audit and feed:**
    * 100 `read` calls within 600 s (fake clock) produce exactly one `artifact_tool_used` event (`calls: 1`, emitted on the first call); a 101st call after 600 s emits a second event with `calls: 100` (the 99 suppressed calls plus itself).
    * `agents.json` lists the external agent's `join_id` and `hard_expiry`, and is rewritten after `revoke_agent`.
    * Replaying the event log with the three new types (and rolling back to code without them) is harmless.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `tests/control_plane/test_collab_external.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): artifact bridge reads and agent feed for collab-public`.

### Task 7b: Chunk staging (`stage_chunk` / `stage_chunk_for`)

**Files:** Modify `herdr_engineering/control_plane/collab_artifacts.py`; Test `tests/control_plane/test_collab_artifacts.py` (extend).

**Interfaces:**
* `stage_chunk(pr, args) -> {upload_id, index, stored: bool, complete?: bool, received?: int, missing?: [int]}`. This is the agent tool path.
  * It needs a `write` grant on `args.artifact_id` and `writable`.
  * Rate limit: `_throttle("artchunk:"+pr.id, limits.chunks_per_minute, 60.0, "chunk")`.
  * It runs `validate_chunk`, then `stage_chunk_for(agent_owner_key(pr.id), chunk, limits=limits, staging_cap=limits.staging_bytes_per_agent)`.
* `stage_chunk_for(owner_key: str, chunk: Chunk, *, limits: BridgeLimits, staging_cap: int) -> dict` is the owner-keyed staging core. It takes no `Principal` and checks no grant; authorization is the caller's job. Plan 5's `MemberService` calls it with a member owner key for agents and web users alike.
  * It derives `upload_id_for(owner_key, …)`.
  * On the first chunk of an upload it reserves the declared `size_bytes`. The reservation is checked against `staging_cap` (the sum of declared sizes in that owner's staging dir) and `staging_bytes_total` (all owners), and a refusal is 429 "staging space full; commit or wait". The `meta.json` (`StagedUpload` with `owner_key`) is written with `link_new_bytes`.
  * A later chunk's declared fields must match `meta.json`, else 409.
  * The part is written with `link_new_bytes`. If the part exists with the same sha, the call is a replay (`stored: false`). If it exists with a different sha, the answer is 409 "chunk already staged with different content".
  * The progress fields `complete`, `received` and `missing` are returned only when `index == total - 1` or on a replay. `missing` holds the first ≤ 100 absent indices.
  * The progress fields come from one directory scan. A plain chunk never scans the directory, so a 190 000-part upload stays linear.
  * `_note_complete(owner_key, upload_id, meta)`: whenever a scan observes the upload complete, it calls `link_new_bytes(upload_dir, "audited", b"")` and audits `artifact_upload_staged` (upload id, artifact, size) only when that returns True, so the event is emitted exactly once per upload, across restarts. Task 7c's `commit()` precheck calls the same function.

- [ ] **Step 1: Write failing tests:**
  * A stages chunks 0 and 2 of 3. The reply to 2 says `complete: false, missing: [1]`.
  * A then stages 1, and a replay of 2 says `complete: true`.
  * A plain chunk reply has no progress fields.
  * The parts are 0600 under `staging/<A>/<upload_id>/`, and `meta.json` carries `owner_key`.
  * Re-sending chunk 1 gives `stored: false`; chunk 1 with different bytes gives 409.
  * Bad base64, a wrong chunk sha and a 524 289-byte chunk each give 400, and nothing is staged.
  * B staging on Y gives 403 "read-only grant".
  * A first chunk whose declared size exceeds `staging_bytes_per_agent` (set small) gives 429.
  * The `chunks_per_minute + 1`th chunk within 60 s gives 429.
  * A chunk naming `artifact_id` Y (no grant) gives 404.
  * `stage_chunk_for("m_test", chunk, …)`, called directly with no principal and no grant, stages under `staging/m_test/` and respects `staging_cap` and `staging_bytes_total`. A replay through it is idempotent too.
  * A completed upload produces exactly one `artifact_upload_staged` event, with no content, even after further replays.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): resumable chunk staging for collaborator uploads`.

### Task 7c: Intents and results (`append_intent` / `read_result` / `commit` / `result`)

**Files:** Modify `herdr_engineering/control_plane/collab_artifacts.py`; Create `herdr_engineering/control_plane/collab_waiters.py` (the per-key waiter counter moved out of `collab_mcp.py:179-181`: `MAX_WAITERS = 2` and a `waiter_slot(key) -> contextmanager` that yields False when the key already holds `MAX_WAITERS` waits); Modify `herdr_engineering/control_plane/collab_mcp.py` (use `collab_waiters` instead of its private `_waiters`/`MAX_WAITERS`, no behaviour change). `collab_waiters` imports neither `collab_mcp` nor `collab_artifacts`. Test `tests/control_plane/test_collab_artifacts.py` (extend).

**Interfaces:**
* `append_intent(actor: Actor, kind: str, payload: dict, *, artifact_id: str | None, idempotency_key: str | None) -> str` is the generic intent write. It returns the ticket.
  * It enforces the registered kind and actor via `validate_intent` on the envelope it builds, and the `max_pending_intents_per_agent` cap per actor.
  * For an agent actor, it refreshes `agents.json` first when the feed is older than 15 s.
  * It records ticket→actor in a bounded in-memory map and writes the intent with `atomic_write_json`.
  * Its callers authorize first. The plan-3 caller is `commit()`; plan 5's `MemberService` is another.
* `read_result(actor: Actor, ticket: str, *, wait: float, still_valid: Callable[[], bool]) -> dict` is the generic long-poll.
  * It checks ownership by the ticket map AND by the `actor` in the result file. A mismatch or an unknown ticket gives the same 404 "not found".
  * It reads the result file with `read_json_beneath(mirror, ["results", f"{ticket}.json"], max_bytes=limits.max_conflict_text_bytes + 1_048_576)`.
  * It polls the file at ≤ 0.5 s intervals up to `wait` (≤ 20 s), under `collab_waiters.waiter_slot` per actor, and stops early when `still_valid()` turns false.
  * It returns `{status: "pending"}` or the result.
* `commit(pr, args) -> {ticket, status: "pending"}`. It needs a `write` grant and `writable`.
  * Limits, in order:
    * `_throttle("artcommit:"+pr.id, limits.commits_per_minute, 60.0, "commit")`;
    * the `max_pending_intents_per_agent` cap;
    * `validate_changes`;
    * for each `upload_id`, its `meta.json` must exist under the **caller's** staging dir for this artifact and name, and `upload_complete` must hold (all parts, or the worker's `verified` marker). Otherwise the answer is 404 "upload not found", or 409 "upload incomplete" with `missing`. A complete upload not yet audited is passed to `_note_complete` (Task 7b). An intent whose `idempotency_key` replays a finished commit may therefore still be refused here after the TTL; the worker-side short-circuit covers retries within the TTL;
    * then `append_intent(Actor("agent", pr.id), "commit", payload, …)`.
  * It audits `artifact_commit_submitted` with ticket, artifact and byte count, never text.
* `result(pr, ticket, *, wait: float) -> dict` is `read_result(Actor("agent", pr.id), ticket, wait=wait, still_valid=lambda: service.still_valid(pr))`.

- [ ] **Step 1: Write failing tests:**
  * **Commits:**
    * B's `commit` on Y gives 403 "read-only grant".
    * The `commits_per_minute + 1`th commit within 60 s gives 429.
    * A 5th pending intent gives 429.
    * A commit with A's incomplete upload gives 409 with `missing`.
    * B committing (on an artifact B can write) with A's `upload_id` gives 404 "upload not found".
    * An upload completed out of order (the final chunk sent was not index `total-1`, so no stage reply scanned) and then committed emits `artifact_upload_staged` exactly once, from the commit precheck.
    * The intent file holds no `Authorization`, credential digest or device id, and no staged bytes.
    * With `agents.json` 200 s old and no request traffic, `commit()` refreshes the feed first, so the worker's freshness check passes.
  * **Generic intents:**
    * `append_intent(Actor("user","u1"), "x.test", …)` for a test-registered kind writes an intent the worker's `validate_intent` accepts.
    * `read_result` for that ticket by `Actor("user","u2")` gives 404, and by `u1` returns the result.
    * `append_intent` with an unregistered kind raises 400 before writing anything.
  * **Results:**
    * B calling `result` with A's ticket gets 404, both immediately and after waiting.
    * `result` returns the worker's file content once it appears mid-wait.
    * A third concurrent wait by one actor is refused by the waiter cap, and the existing `collab_mcp` wait tests still pass with the moved counter.
    * A result file larger than `max_conflict_text_bytes + 1 MiB` is not parsed (404/502, no partial read).
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `tests/control_plane/test_collab_external.py` and `tests/control_plane/test_collab_bounded.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): collaborator commit intents and result long-poll`.

### Task 8a: MCP tool schemas and `_call`

**Files:** Modify `herdr_engineering/control_plane/collab_mcp.py`: `_ARTIFACT_TOOLS`, `ARTIFACT_TOOL_NAMES`, `tools_for`, `_call`. Test `tests/control_plane/test_collab_mcp_artifacts.py`.

**Interfaces:**
* Tool schemas follow spec B's table, plus the seventh tool. All of them have `additionalProperties: False`.
  * `artifacts_list {}`, `artifact_head {artifact_id}`, `artifact_read {artifact_id, name, version_id?, offset?, max_bytes? (1..1048576)}`, `artifact_history {artifact_id, limit? (1..200)}`.
  * `artifact_stage_chunk {artifact_id, name, size_bytes (≥1), file_sha256, index (≥0), total (≥1), sha256, data_b64}`.
  * `artifact_commit {artifact_id, base_version_id, message (≤1000), changes (array of {name, op: put|delete, text?, upload_id?}), idempotency_key?}`. The changes array has no schema `maxItems`, because `max_changes_per_commit` is configurable and enforced in code.
  * `artifact_commit_result {ticket, seconds? (0..20)}`.
* `tools_for(service, pr)` appends `_ARTIFACT_TOOLS` only when `service.artifact_bridge` is set and `artifact_bridge.has_grants(pr)` holds (so the tools are hidden while the feed is stale).
* `_call` handles artifact tool names **before** the `room_id` check (collab_mcp.py:303):
  * `service.artifact_bridge is None` → error "unknown tool".
  * Bridge set but `not artifact_bridge.available()` (stale, missing or invalid `grants.json`) → 503 "collaboration bridge unavailable".
  * Bridge available and `not has_grants(pr)` → "unknown tool".
  * Otherwise dispatch to the bridge method.
* Wrapping: every artifact tool result is wrapped `untrusted=True` — contents, history, results, the `head` and `list` manifests (names chosen by others), the commit-ticket reply and the stage-chunk reply.

- [ ] **Step 1: Write failing tests:**
  * **Tool listing:**
    * An agent without grants gets a `tools/list` with none of the seven tools, and `tools/call artifacts_list` gives the error "unknown tool".
    * A granted agent gets all seven.
    * No tool name in any agent's list contains `share`, `acl` or `grant`, for internal and external agents with every grant combination.
  * **Stale feed (through MCP):** a granted agent with `grants.json` older than 120 s sees no artifact tools in `tools/list`, and `tools/call artifacts_list` answers 503 "collaboration bridge unavailable", not "unknown tool"; with no bridge configured the same call answers "unknown tool".
  * **Arguments and wrapping:**
    * An unknown argument gives 400 for every tool.
    * `artifact_read` output starts with the `UNTRUSTED` notice, and so do the `artifact_commit` and `artifact_stage_chunk` replies.
    * An artifact tool call without `room_id` reaches the artifact branch (not "room_id is required").
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `tests/control_plane/test_collab_external.py`, `test_collab_pentest.py` and `test_collab_bounded.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): artifact collaboration MCP tools`.

### Task 8b: The `/mcp` large-body gate, admission registry and edge rate limits

**Files:** Modify:
* `herdr_engineering/control_plane/collab_mcp.py`: `register_large_body_admission`, and let `handle()` accept an already-authenticated principal;
* `herdr_engineering/control_plane/api.py`: `_jsonrpc` (api.py:2112), `_rate_limit` (around api.py:2850) and a new `self._mcp_limiter = _RateLimiter(max_requests=1800)` beside `_ingest_limiter` (api.py:518);
* `deploy/control-plane/collab-edge/nginx.conf`: a `limit_req_zone $remote_addr zone=hcn_mcp:10m rate=25r/s;` in `http`, and in `location = /mcp` `client_max_body_size 768k; limit_req zone=hcn_mcp burst=100 nodelay;`.

Tests: `tests/control_plane/test_collab_mcp_artifacts.py` (extend), `tests/test_share_deploy_config.py` or the existing nginx config test (`grep -rl client_max_body_size tests/`).

**Interfaces:**
* Request bodies in `_jsonrpc`, for label `"MCP"` only:
  * A `Content-Length` ≤ `MAX_BODY` behaves as today.
  * Admission registry: `register_large_body_admission(predicate: Callable[[CollabService, Principal], bool], tools: frozenset[str])` in `collab_mcp.py`. Plan 3 registers `(lambda s, pr: s.artifact_bridge is not None and s.artifact_bridge.has_write_grant(pr), {"artifact_stage_chunk"})`. Plan 5 registers `(member_check, {"artifact_upload_chunk"})`, which admits member agents with no write grant.
  * For `MAX_BODY` < length ≤ `MCP_UPLOAD_MAX_BODY` (Task 3), it **authenticates first** (`service.authenticate` from headers). It computes the union of `tools` over every admission whose predicate holds.
    * If the union is empty, it answers 413 without reading **and with `Connection: close`**, so unread bytes are never parsed as a next request (the share routes' precedent).
    * Otherwise it reads the body.
  * It passes the principal it authenticated into `collab_mcp.handle`, which then does not authenticate a second time, so failure stats are counted once.
  * After parsing, a body over `MAX_BODY` whose `params.name` is not in that union is answered with JSON-RPC error `-32602 "request too large for this tool"`.
  * Anything over `MCP_UPLOAD_MAX_BODY` gives 413 for everyone.
  * Plan 5 extends this same gate through the registry. It must not add a second one.
* Rate ceilings (Global Constraints): `_rate_limit` sends `POST /mcp` to `server._mcp_limiter` (1800/min per client) instead of `server._limiter` (600/min); every other route is unchanged. nginx: `/mcp` uses `hcn_mcp` (25 r/s, burst 100) instead of the inherited `hcn_req`; the server-level `client_max_body_size 64k` and `limit_req zone=hcn_req` stay for every other location.

- [ ] **Step 1: Write failing tests:**
  * **Body limits:**
    * A 700 KiB body from an unauthenticated caller gives 413 with `Connection: close`, and the body is never read: assert with an `rfile` that raises if read past 0 bytes.
    * The same body from an agent with only read grants gives 413.
    * A write-granted agent's 700 KiB `artifact_stage_chunk` (one full 512 KiB chunk, base64) is accepted.
    * A write-granted agent's 700 KiB `artifact_commit` gives `-32602`.
    * A test-registered admission (always true, tools `{"x_chunk"}`) admits a 700 KiB `x_chunk` from an agent with no grants, and gives `-32602` for its 700 KiB `artifact_stage_chunk`. The registry is reset afterwards.
    * 800 KiB gives 413 for everyone.
    * A failed authentication on a large body is counted once in the auth-failure stats.
  * **Rate ceilings:**
    * The first 1800 `POST /mcp` calls from one client within 60 s are admitted by the server limiter and the 1801st is 429, and a dashboard route from the same client is still governed by the 600/min limiter.
    * The nginx config test asserts: the 768k limit appears only in `location = /mcp`; `zone=hcn_mcp` is defined at `rate=25r/s` and used only in `location = /mcp` with `burst=100`; the server-level `hcn_req` 10r/s/burst 40 is unchanged.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the tests plus `tests/control_plane/test_collab_external.py`, `test_collab_pentest.py` and `test_collab_bounded.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): chunk body gate and /mcp rate ceilings for collaborator staging`.

### Task 8c: End-to-end bridge test

**Files:** Create `tests/e2e/test_artifact_collab_bridge.py`.

**Interfaces:** It uses Postgres, a fake RustFS, a temp mirror/outbox, an in-process `CollabService` and `CollabArtifactBridge`. It drives `collab_mcp.handle` plus `CollabMirror.publish_grants`, `CollabMirror.sync`, `CollabIntentApplier.tick` and `sweep_staging` in one process.

- [ ] **Step 1: Write the test**, covering:
  * A grants write on X and the owner commits v2.
  * A reads v1 via MCP and commits a disjoint edit with base v1. A `merged` result arrives through `artifact_commit_result`, and `artifact_history` shows the merge with two parents.
  * A then commits an overlapping edit and gets `conflict` with markers, resolves it, and commits with `base = head_version_id` to get `created`. Activity (`CollabGrantStore.attempts`) lists the conflict names.
  * A stages a 1.3 MiB binary file in 3 chunks, interrupted after chunk 2 and resumed from `missing`. A commits it with `upload_id` and gets `created`, and the manifest shows its sha256 and size.
  * The owner revokes. Within 2 ticks `artifacts_list` is empty, and a commit is refused 404 in collab-public, or `rejected` "no write grant" if it was already queued. A's leftover staging for X is swept.
- [ ] **Step 2: Run** and expect PASS (fix product code in the owning task's module if it fails).
- [ ] **Step 3: Commit** `test(collab): artifact collaboration end to end`.

### Task 9: Wiring, deployment and the fast worker loop

**Files:** Modify:
* `herdr_engineering/control_plane/worker.py`: add `CollabBridgeLoop`;
* `herdr_engineering/cli.py`: `_collab_artifacts_settings(cp)` (which builds `BridgeLimits.from_mapping`), `_cmd_cp_worker` (start the loop, build the applier with `lineage=collab_lineage_poster_from_section(worker_cfg.get("lineage"))`), and the collab-only server flags `--collab-artifacts-mirror` / `--collab-artifacts-outbox`, which construct `CollabArtifactBridge` only when the collab slice's `collab_artifacts.enabled` is true;
* `herdr_engineering/fleet_config_cli.py`: `render_collab` additionally renders `collab_artifacts: {enabled: <bool>}` (exactly `true` → true, anything else → false) into the slice's control-plane values, beside `collab` (`fleet_config._parse` does not allowlist value keys, so no schema change is needed);
* `deploy/control-plane/compose.yaml`: worker mounts;
* `deploy/control-plane/compose.collab.yaml`: `collab-public` mounts and the two static flags (always present; they do nothing unless the slice enables the bridge);
* `deploy/control-plane/deploy.sh`: export and **always** create `HERDR_COLLAB_MIRROR_DIR`, `HERDR_COLLAB_OUTBOX_DIR` and `HERDR_COLLAB_SPOOL_DIR` beside the share dirs on large storage;
* `deploy/control-plane/fleet-config.example.yaml`: `control_plane.collab_artifacts` with every key of the Configuration table, commented with its default.

Tests: `tests/control_plane/test_worker.py` (extend), `tests/test_compose_render.py`, `tests/test_central_config_servers.py` (render-collab slice), `tests/test_share_deploy_config.py` and `tests/test_deploy_scripts.py` (extend).

**Interfaces:**
* `CollabBridgeLoop(dsn_connect: Callable[[], Awaitable[Connection]], mirror: CollabMirror, applier: CollabIntentApplier, *, interval=1.0, mirror_every=30.0, retractions_every=30.0, heartbeat=30.0)` with `async run(stop: asyncio.Event)`. It runs three tasks, and none uses the worker pass's connection:
  * **Grants task**, on its own connection, every `interval` (1 s): `publish_grants(heartbeat=heartbeat)`, `resolve_invite_grants` from `agents.json` (Task 1), and the `load_retractions` refresh every `retractions_every`. It never awaits a mirror refresh, so the ~2 s revocation and the 120 s freshness do not depend on refresh duration.
  * **Mirror task**, on its own connection: `mirror.sync` (every `mirror_every`, or at once for artifacts whose grants changed), `sweep_staging` and `sweep_results`. A long refresh delays only this task.
  * **Applier task**, on a small pool of `limits.apply_concurrency` connections. It runs `applier.tick` every `interval`.
  * Each task logs its own exceptions and reconnects after 5 s. A failing task never stops the others.
* `_cmd_cp_worker` runs it as a separate `asyncio` task beside `run_worker` when `collab_artifacts.enabled is True`. It builds the applier with `context_extras={}`, `lineage=collab_lineage_poster_from_section(...)` (None when the section is absent) and registers only `commit`; plan 5 adds its kinds, extras and staging owner here. An invalid limits section fails the worker start with a clear message. In `--once` mode the loop is not started (it is a long-running loop; `--once` runs one claim cycle as today).
* collab-public: builds `CollabArtifactBridge` and sets `service.artifact_bridge` only when the rendered slice has `collab_artifacts.enabled: true` **and** both mount flags are present; otherwise `artifact_bridge` stays None and the MCP tools say "unknown tool".
* Mounts:
  * The worker mounts `/collab-mirror` rw, `/collab-outbox` rw and `/collab-spool` rw.
  * `collab-public` mounts `/collab-mirror:ro` and `/collab-outbox` rw.
  * Neither sees the other's private volumes. `collab-public` still has no Postgres or RustFS.
* Directory ownership (deploy.sh runs as the deploying uid and cannot chown to root): mirror and outbox are created as the deploying uid with mode 0755, with the existing uid-mismatch warning against `HERDR_COLLAB_CERT_UID` (as for the share dirs, deploy.sh:513-519); the spool dir is created 0700 (the root worker ignores the mode).

- [ ] **Step 1: Write failing tests:**
  * **Loop:**
    * The grants, mirror and applier connections differ from each other and from `run_worker`'s (injected fakes).
    * A failing tick in any task does not stop the others.
    * With an injected short heartbeat (e.g. 0.2 s) and a fake clock, a fake `put_stream` that blocks for 5 s does not stop `grants.json` being rewritten on its heartbeat, and a second agent's intent is applied.
    * With a slow fake `iter_get` (a mirror refresh that takes 5 s), `grants.json` keeps its heartbeat and a revoke reaches `grants.json` within ~2 s.
    * The applier is built with the lineage poster from `artifact_worker.lineage`; with the section absent, `lineage` is None.
    * With `enabled` absent or `false`, no loop starts; the render-collab slice says `collab_artifacts: {enabled: false}` and `collab-public` starts no bridge (even though the mount flags are present). With `enabled: true` the slice says true and the bridge is built.
    * `--once` does not start the loop.
  * **Config:**
    * `daily_bytes_per_agent` from YAML reaches the applier and `grants.json` `limits`.
    * `daily_bytes_per_agent: 0` fails the start.
    * No `os.environ` read is introduced: grep-style test over the new modules.
  * **Compose and deploy:**
    * The compose render keeps the mount table in step: `collab-mirror` is `:ro` in `collab-public` and rw in the worker, the `outbox` mount is present in both, and `collab-spool` is in the worker only. `collab-public` still has no `env_file`, no Postgres network and no new capabilities. The `collab-public` `command` list, including the two static flags, matches the command-parity test.
    * `deploy.sh` always creates the mirror and outbox dirs with mode 0755 and warns on a uid mismatch with `HERDR_COLLAB_CERT_UID`, and creates the spool dir 0700, all beside the share dirs.
    * The worker (which has no `user:`, so it runs as root) writes mirror files 0644 and dirs 0755, and can read 0600 staged parts and unlink in the outbox. A deploy test pins these modes.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the extended tests plus `tests/test_deploy_scripts.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(deploy): collaborator bridge directories and the 1 s worker loop`.

### Task 10a: Owner routes for collaborators, activity and version diffs, with web wiring

**Files:** Create `herdr_engineering/control_plane/artifact_collab_api.py` (hook `route_collab` into `_route_api` beside `route_share`, api.py:2884); Modify:
* `herdr_engineering/control_plane/artifacts.py`: `list_version_summaries` adds `provenance->>'message' AS message`; add `version_text_diff`;
* `herdr_engineering/control_plane/api.py`: `ControlPlaneServer(..., collab_grant_store=None, collab_artifacts_enabled: bool = False)` stores both on the server;
* `herdr_engineering/cli.py`: `_collab_grant_wiring(cp, dsn) -> (adapter | None, enabled: bool)` following `_share_wiring` (cli.py:3134): when `control_plane.collab_artifacts.enabled is True` and a DSN exists, a `SyncArtifactAdapter` over `async def create(): return CollabGrantStore(await asyncpg.connect(dsn))`; `_cmd_cp_web` passes both to `ControlPlaneServer`.

Tests: `tests/control_plane/test_artifact_collab_api.py`.

**Interfaces:** All routes need `admin` on the artifact (via `_access`, imported from `artifact_report_api.py:71`), except `diff`, which needs `read`. Denials are a non-disclosing 404. A POST with a foreign `Origin` gets 403. When `server.collab_artifacts_enabled` is not true (or `server.collab_grant_store` is None), every route answers 503 "collaboration is not enabled". Store calls go through `server.collab_grant_store("method", …)` (the adapter's call path).
* `GET /api/v1/artifacts/{id}/collaborators` returns `{writable, writable_reason, collaborators: CollabGrantStore.collaborators(id), pending_invites: [...]}`.
* `POST …/collaborators` takes `{agent_id, permission: read|write, expires_in_days: 1..90 = 30}` and returns 201.
  * `write` on a non-writable artifact gives 409 with `writable_reason`.
  * Internal and external agents are both accepted for `write` (Decision 5).
  * An `agent_id` must match `^[A-Za-z0-9_.:-]{1,80}$`.
  * An artifact without `created_by` gives 409 "legacy artifact: set an owner first".
* `POST …/collaborators/{agent_id}/revoke` returns 200.
* `POST …/collaborators/invite-grant` takes `{invite_id, permission, expires_in_days}` and returns 201. It stores a pending invite grant; the GUI calls it right after creating the invite through the existing `/api/v1/collab/rooms/{rid}/externals/invites` proxy.
* `GET …/collab-activity` returns `{attempts: CollabGrantStore.attempts(id)}`.
* `GET …/versions/{vid}/diff` returns `{version_id, against, files: [{name, change: added|deleted|modified|unchanged, diff: str|null, reason?: binary|oversize}]}`.
  * `against` is the version's first parent.
  * Implemented as `ArtifactStore.version_text_diff(artifact_id, version_id) -> dict`. It reuses `ArtifactBundle.diff(left, right, media_type=…, max_lines=…)` (artifact_bundle.py:36) for the text; it does not call `difflib` itself. Before diffing it skips a file over 2 MiB (either side) with `reason: oversize` and stops diffing once the response holds 4 MiB of diff text (the rest get `reason: oversize`). A non-text file gets `reason: binary`. These caps are for display only: they bound diff cost and are not storage limits.

- [ ] **Step 1: Write failing tests:**
  * **Wiring:** with `collab_artifacts.enabled` absent, `_collab_grant_wiring` returns `(None, False)` and every route answers 503; with it true, the server holds the adapter and the flag.
  * **Access:** a non-admin caller gets 404 on every route; a cross-origin POST gets 403.
  * **Grants:**
    * A grant writes the `collab:` row and the web's in-memory `server.artifact_acl` is unchanged.
    * An internal agent can be granted `write`.
    * `write` on a report gives 409.
    * `expires_in_days: 91` gives 400, an unknown body key gives 400, and an omitted `expires_in_days` gives 30 days.
    * Revoke removes the row.
  * **Activity, diffs and summaries:**
    * Activity lists a conflict with its names.
    * The diff of a merge version is against its first parent, and a binary file gets `reason: binary`; the diff text equals `ArtifactBundle.diff`'s output for that file.
    * A hostile file name round-trips as data (JSON), never interpreted.
    * Version summaries include `message`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_share_api.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): owner routes for collaborators, activity and diffs`.

### Task 10b: Retraction fail-safe and the optional restrict-all

**Files:** Modify:
* `herdr_engineering/control_plane/artifact_retraction.py`: `retract(..., on_unpersisted=None)`;
* `herdr_engineering/control_plane/api.py`: `_artifact_retract` (api.py:5193) widens its body allowlist from `{version_id, reason}` to `{version_id, reason, restrict_all}` (`restrict_all` must be a bool, else 400), passes the fail-safe and honours `restrict_all`;
* `herdr_engineering/control_plane/artifact_collab_api.py`: `POST …/collab-restrict`.

Tests: `tests/control_plane/test_artifact_graph_routes.py` (extend; the retract route tests live there), `tests/control_plane/test_artifact_collab_api.py` (extend).

**Interfaces:**
* `retract(..., on_unpersisted: Callable[[str], int] | None = None)` calls `on_unpersisted(artifact_id)` only when `persist` raised.
* The result gains `collab_write_grants_revoked: int | None` and `collab_revoke_error: str | None`. A failing callback is caught and reported, and the route still answers 500 with both errors.
* `_artifact_retract` passes a callback that runs `revoke_write_grants(artifact_id)` through `server.collab_grant_store` (Task 10a's adapter); when the adapter is None (bridge disabled) no callback is passed. The worker's next `publish_grants` then drops those grants from `grants.json`.
* The callback runs for version retractions as well as artifact retractions.
* Optional restrict-all (operator action only, never automatic):
  * When `persist` raised, the 500 body also carries `restrict_all_offered: true`, so clients can ask "restrict all access now?".
  * `POST /api/v1/artifacts/{id}/collab-restrict` (needs `admin`; 503 when disabled) runs `CollabGrantStore.revoke_all_grants(artifact_id)` and returns `{revoked: int}`. The worker's next `publish_grants` and mirror sync then drop the grants and remove the artifact from the mirror.
  * `_artifact_retract` accepts `restrict_all: true` in the body and then runs the same revocation after the write-grant fail-safe. Without that flag, read grants are never touched.

- [ ] **Step 1: Write failing tests:**
  * **Retraction fail-safe:**
    * When `persist` raises, the callback runs. The artifact's write grants are gone and its read grants remain, and the result carries `collab_write_grants_revoked: 1`.
    * When `persist` succeeds, the callback never runs.
    * When the callback raises, the result carries both errors and is a 500.
    * `restrict_all: "yes"` gives 400; an unknown body key still gives 400.
  * **Restrict all (optional):**
    * A failed-persist retraction without `restrict_all` leaves read grants and answers `restrict_all_offered: true`.
    * With `restrict_all: true`, a failed-persist retraction also removes the read grants.
    * `POST …/collab-restrict` removes read and write grants of that artifact only, needs `admin` (a non-admin gets 404, a cross-origin POST gets 403), and the mirror drops the artifact within one sync.
    * No code path calls `revoke_all_grants` automatically: a successful retraction, and a failed one without the flag, never do.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_graph_routes.py` and `test_artifact_collab_api.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): retraction fail-safe revokes collaborator write grants`.

### Task 11: Owner CLI

**Files:** Create `herdr_engineering/artifact_collab_client.py` with `add_collab_subparser(asub)`, registered in `cli.py` under the existing `artifacts` parser (cli.py:308) after `add_share_subparser`; Test `tests/test_artifact_collab_client.py` (follow the share client tests).

**Interfaces:** These call Task 10a/10b's routes with the CLI's existing API-key client and print JSON:
* `herdr-eng artifacts collab list <artifact_id>`;
* `… grant <artifact_id> <agent_id> [--write] [--days N]` (1–90, default 30);
* `… revoke <artifact_id> <agent_id>`;
* `… restrict <artifact_id>` calls `POST …/collab-restrict`;
* `herdr-eng artifacts retract <artifact_id> [--version-id V] [--reason R] [--restrict-all]` is a **new** command (none exists today). It calls `POST /api/v1/artifacts/{id}/retract` with `{version_id?, reason?, restrict_all?}`, sending `restrict_all: true` only with `--restrict-all`, and prints the JSON result (including `restrict_all_offered` on a 500).

- [ ] **Step 1: Write failing tests:** each command hits the exact path, method and body (`retract` sends `version_id`/`reason` only when given and `restrict_all` only with `--restrict-all`), and `--days 0` and `--days 91` are refused locally. **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS. **Step 5: Commit** `feat(cli): artifacts collab grant/list/revoke/restrict and artifacts retract`.

### Task 12: Share panel Collaborators and Activity tabs

**Files:** Modify:
* `packages/herdr-web-client/client.js`: add the artifact collaboration methods only;
* `packages/artifact-share-panel.js`: tabs Links | Collaborators | Activity, where the Links tab is today's content unchanged.

Create `packages/artifact-collaborators.js` and `packages/artifact-activity.js`. Tests: `packages/herdr-web-client/artifact-collab.test.js`, `packages/artifact-collaborators.test.js`, `packages/artifact-activity.test.js`.

**Interfaces:**
* Client methods in `client.js` (all `{signal} = {}`): `listArtifactCollaborators(id)`, `grantArtifactCollaborator(id, {agent_id, permission, expires_in_days})`, `revokeArtifactCollaborator(id, agentId)`, `grantArtifactInvite(id, {invite_id, permission, expires_in_days})`, `artifactCollabActivity(id)`, `artifactVersionDiff(id, versionId)`, `restrictArtifactCollab(id)`.
* Collab calls reuse the existing `CollabApi` (`packages/herdr-web-client/collab.js`): `agents()`, `rooms()` and `inviteExternal(room, body)` on the dashboard proxy's `/api/v1/collab` base. No new `client.js` methods duplicate them. `CollabApi.call` currently requires a bearer token from `getTokens`; the dashboard runs same-origin with no token in storage, so add an optional constructor mode (e.g. `getTokens = null`) in which `call` sends no `Authorization` header and uses the injected `fetch`, rather than copying the methods. Existing `CollabApi` users are unchanged.
* Renderers, pure, returning escaped HTML (no inline handlers, no iframes):
  * `renderCollaboratorRow(c, {busy})` shows agent, permission, expiry, last used, commits, conflicts and Revoke.
  * `renderGrantForm(state)` has an agent `<select>`, a Read/Read+write choice disabled with `writable_reason` when not writable, and days 1–90 (default 30).
  * `renderInviteResult(invite)` shows the code once, with the same once-only and clear-on-close rules as the share URL.
  * External invites are room-bound, so the invite form has a room `<select>` filled from `CollabApi.rooms()`.
  * `renderRestrictAll(state)` shows a **Restrict all access** button with a confirm step. It is offered in the Collaborators tab, and is highlighted when a retraction answered `restrict_all_offered`. It is never triggered automatically.
  * `renderHistoryRow(v)` shows number, author label, message, parents and a merge badge.
  * `renderAttempt(a)` shows outcome, agent, conflict names and kinds, reason and time.
* Mounting:
  * `mountCollaborators(container, client, collabApi, artifactId, {signal})` and `mountActivity(container, client, artifactId, {versions, signal})`.
  * Activity's "Show rejected attempts" toggle loads lazily.
  * Selecting a version renders the file list and `renderDiff` (from `herdr-web-components/inspector.js:105`) for each modified file. A file with `reason: binary|oversize` shows the reason instead of a diff.

- [ ] **Step 1: Write failing tests:**
  * The client hits exact paths and bodies, with ids `encodeURIComponent`-ed.
  * `CollabApi` in token-less mode sends no `Authorization` header and hits `/api/v1/collab/agents`, `/rooms` and `/rooms/{r}/externals/invites`; its token mode behaves as before.
  * A hostile agent name, message, file name or conflict name (`<img onerror>`) is escaped.
  * A non-writable artifact disables Read+write and shows the reason.
  * The Restrict all access button needs a confirm, then calls `restrictArtifactCollab` once; nothing calls it without a click.
  * A 503 from `listArtifactCollaborators` shows "Collaboration is not enabled" with the form hidden, and the Links tab still works.
  * A failed `CollabApi.agents()` shows an agent-id text input instead of the picker.
  * The invite code disappears from the DOM on Close.
  * The rejected toggle fetches only once.
  * A merge version shows two parents and the badge.
  * A binary file in a diff shows "binary" with no diff body.
  * Buttons are disabled while their call is in flight.
- [ ] **Step 2: Run** `node --test` on the three files and expect FAIL. **Step 3: Implement.** **Step 4: Run** `npm run test:client` and expect PASS.
- [ ] **Step 5: Commit** `feat(web): collaborators and activity tabs`.

### Task 13: Playwright end-to-end for the tabs

**Files:** Extend `tests/playwright/artifact-shares-server.py` (in-memory collaborators, activity, diff and collab agents/rooms routes) and `tests/playwright/artifact-shares.test.mjs`.

- [ ] **Step 1: Write the test:**
  * Open the Collaborators tab, pick an agent, choose Read+write and grant: the row appears.
  * Revoke: the row disappears.
  * A report artifact shows the disabled write option with its reason.
  * Invite + grant shows the code once; after Close, `page.content()` lacks it.
  * Activity shows the merge badge.
  * The rejected toggle lists a conflict.
  * Selecting a version shows the diff lines.
  * A hostile title or agent name does not execute (`window.__x` stays undefined).
  * The collaborators route returning 503 leaves Links usable.
- [ ] **Step 2: Run** `npm run test:ui` for this file, FAIL then PASS. Fix product code if the test shows a defect.
- [ ] **Step 3: Run** `npm run test:ui-gates` for the artifact surface. Update only intentionally changed baselines, using the repo's procedure. **Step 4: Commit** `test(web): collaborators and activity end-to-end`.

### Task 14: Documentation

**Files:** Modify:
* `packages/collab-guide.md` and its mirror `docs/operations/collaboration-network-guide.md`. Keep them identical; the parity test is `tests/control_plane/test_collab_public.py`.
* `skills/herdr-artifact/SKILL.md`: a short "collaborating through MCP" reference.
* `deploy/control-plane/COLLAB.md`: the bridge, staging and spool dirs on large storage (created by deploy.sh as the deploying uid; mirror/outbox 0755, spool 0700), the enable switch (`control_plane.collab_artifacts.enabled`, rendered into the collab slice), the full `control_plane.collab_artifacts` key table (including `mirror_max_manifests`), the `/mcp` rate ceilings, and rollback = `enabled: false`.

- [ ] **Step 1:** Document:
  * the seven tools with arguments and results;
  * the staging loop:
    * chunk the file at 512 KiB and send each chunk with its sha256;
    * resume from `missing`;
    * commit with `upload_id`;
    * know that a re-sent chunk is harmless;
  * when inline `text` is enough (small text under the 64 KiB request) and when to stage (anything larger, and binary files);
  * the commit/merge/conflict loop: resolve, then re-commit with `base = head_version_id`. Binary and over-`max_text_merge_bytes` files changed on both sides conflict as `binary`/`oversize` with no merged text. Reusing an `idempotency_key` for a different request is rejected;
  * the limits, as configurable defaults: 100 changes per commit, 30 commits/min, 1200 chunks/min per agent, 100 GB/day, 60 s intent age, a 24 h staging TTL, plain-artifact-only writes;
  * the effective `/mcp` ceiling: 25 requests/s per source IP (about 12.5 MiB/s of chunks), shared by every agent behind one IP or NAT;
  * the honest promise that revocation takes effect within seconds;
  * that collaborators can never share.
- [ ] **Step 2: Run** `tests/control_plane/test_collab_public.py` and any other guide test (`grep -rl collab-guide tests/`) and expect PASS. **Commit** `docs(collab): artifact collaboration tools`.

---

## Contract for plan 5 (member-owned artifacts)

Plan 5 (`2026-10-30-member-owned-artifacts.md`, "Needed from plan 3") depends on the following. Plan 3 lands these extension points with tests and registers nothing for members.

| # | Plan 5 needs | Provided by plan 3 |
|---|---|---|
| 1 | Intent kinds through a handler registry | `register_intent_kind` (Task 3) and `CollabIntentApplier.register(kind, handler, *, lane_key, replay_outcomes)` with `IntentContext` (Task 5a) |
| 2 | A web user as intent sender | `Actor(kind="user")` in the envelope, the ledger and results (Tasks 1, 3, 5a and 7c). `commit` stays agent-only. |
| 3 | One member's intents serial | `lane_key` per kind (Task 5a). Plan 5 maps every actor of a member to `member:<user_id>`. |
| 4 | Sweep keeps member staging | `register_staging_owner("m_", keep)` (Task 5d). Plan 5 supplies `keep` (member valid). |
| 5 | Intent write and long-poll for any kind | `CollabArtifactBridge.append_intent` / `read_result` (Task 7c) |
| 6 | `/mcp` 768 KiB gate admits member agents | `register_large_body_admission(member_check, {"artifact_upload_chunk"})` (Task 8b) |
| 7 | Staging keyed by owner, with no grant | `stage_chunk_for(owner_key, chunk, *, limits, staging_cap)` (Task 7b), `StagedUpload.owner_key` / `upload_complete(root, owner_key, …)`, and `agent_owner_key` refuses `m_` (Task 3) |
| 8 | Member assembly not charged to the 100 GB/day agent quota | `ctx.assemble(..., charge=False)` and `artifact_collab_uploads.charged` (Tasks 1 and 5c) |
| 9 | Share module never imported by plan 3's tick | Task 5a's subprocess test runs with only plan 3's registrations. Plan 5's `member.share_*` handlers import the share module lazily, inside the handler. |

Plan 5 supplies `share_store` and `server` (for `approve_share`) through `CollabIntentApplier(context_extras=…)`, which Task 9 wires as an empty mapping until plan 5 fills it. Plan 5 writes `actor` as a dict and calls `read_result(ticket, wait)`; it adapts to plan 3's `Actor` dataclass and `read_result(actor, ticket, *, wait, still_valid)`, so plan 3 needs no change for that.

**Not provided by plan 3, so plan 5 owns it:** `write_secret_result(ticket, payload)` (0600, read-once). Plan 3's results live in the mirror, which is read-only in `collab-public`. The worker runs as root, so a 0600 file it writes is unreadable by `collab-public`'s uid, and `collab-public` cannot delete it to make it read-once. Plan 5 must design that path itself, for example a per-ticket file collab-public can read plus a deletion request through the outbox.

**Plan 4** registers `annotation_resolve` through the same registry, instead of widening `Intent.kind` to a `Literal`. Plan 4's `artifacts/<id>/annotations.json` survives `CollabMirror.refresh` (Task 4 deletes only unreferenced files in `blobs/`).

## Open questions for the operator

None. The 2026-10-30 questions are resolved under Decisions, and the pre-flight rulings R1–R17 are applied.

## Self-review notes

* **Task list:** 1, 2, 3, 4, 5a, 5b, 5c, 5d, 6, 7a, 7b, 7c, 8a, 8b, 8c, 9, 10a, 10b, 11, 12, 13, 14 (pre-flight R17 split old Tasks 5, 7, 8 and 10; the order is unchanged and every other number is kept, so plan 5's references to "plan 3 Task 3" and "Tasks 5/8" still point at the right areas).
* **Spec coverage:**
  * B's tools are covered by Tasks 7a–7c and 8a–8c; the seventh tool, `artifact_stage_chunk`, by Tasks 3, 7b and 8a/8b (Decision 1).
  * The mirror is Task 4. The intents queue and fast loop are Tasks 3, 5a and 9. The worker as authority is Tasks 5a–5c, which also do staging assembly and verification; the sweeper is Task 5d.
  * `collab:` ACL rows: Tasks 1–2 and 10a. Commit semantics and merge rules: plan 2's `merge_commit`, driven by Task 5b. Large and binary files take the whole-file path, with `binary`/`oversize` conflicts (Decision 2, Task 5c).
  * Scope (writes to plain file artifacts only): Tasks 4, 5b and 10a.
  * History model: plan 2, plus `message` in Task 10a.
  * Limits (all configurable, Decisions 3–4): Tasks 3 (`BridgeLimits`), 4 (published), 5b/5c and 7b/7c (enforced) and 9 (YAML). The fixed `/mcp` per-IP ceilings are in Task 8b and documented in Task 14.
  * Audit and safety: Tasks 3, 5a–5d, 7a–7c and 8a/8b. Owner revert: plan 1.
  * Docs: Task 14. CLI: Task 11. `agents.json`: Task 7a, consumed in Tasks 5a and 9. The invite+grant step: Tasks 1, 7a, 9, 10a and 12.
  * Retraction fail-safe and the optional, operator-driven restrict-all (Decision 7): Tasks 1 (`revoke_write_grants`, `revoke_all_grants`), 10b, 11 and 12.
  * Section C is covered by Tasks 10a, 12 and 13.
  * Out of scope: annotations (plan 4) and member-owned artifacts (plan 5). Hosting is unchanged (Decision 8).
* **Spec deviations:**
  * Operator-approved (2026-10-30):
    * There are seven tools, not six.
    * The spec's 2 MiB-per-commit and text-only writes are replaced by chunked staging of any content; `max_text_merge_bytes` bounds only text merging.
    * The 50 MiB quota becomes 100 GB/day and is configurable.
  * Recorded by the pre-flight (R16):
    * "Every tool call audited": allowed reads are coalesced to one `artifact_tool_used` per (agent, artifact, tool) per 600 s, and each coalesced event carries a `calls` count, so every call is accounted for. Denials and commits are audited individually.
    * "Results wrapped `untrusted=True`": followed for every tool, including the ticket and stage-chunk replies (no deviation remains).
    * "Manifests for all versions": the mirror caps manifests at the configurable `mirror_max_manifests` (default 100 000, effectively all); there is no hard-coded cap.
    * "`agents.json` carries group membership": deferred to plan 5's `member-directory.json`; plan 3 does not need it.
* **Retraction:**
  * The durable state and the worker-local gate are checked in Task 5b. The web's in-memory gate is checked at lineage emission in Task 6.
  * If the web hides an artifact but fails to persist the retraction, the route revokes the artifact's collaborator write grants (Task 10b). Commits stop within one grants-task tick.
  * The remaining residual is a failed revoke, which is reported and normally coincides with Postgres being unreachable, which also blocks `merge_commit`.
  * Read access in that window stays until the operator chooses "restrict all access now" (Decision 7); it is never revoked automatically.
* **Pre-flight rulings applied:** R1 (Task 1, Global Constraints, plans 4/5 renumbered to 0046/0047), R2 (Task 5b), R3 (shared applier design, step order 1–12), R4 (Tasks 10b, 11), R5 (Task 3 helpers; callers in 5a/5c/5d), R6 (Tasks 4, 9), R7 (Global Constraints, Task 9), R8 (Tasks 6, 9), R9 (Task 10a), R10 (Global Constraints, Task 8b, Task 14), R11 (Global Constraints, Task 7a), R12 (Tasks 7a, 8a), R13a–e (Tasks 1, 3, 5a, 5b, 7a, 7b, 7c), R14 (Tasks 3, 4, 7c, 10a, 12; precedent names in the header), R15 (Task 9, Task 14), R16 (Task 4 config, Task 7a feed, Task 8a wrapping, Spec deviations), R17 (task splits).
* **Cross-plan contract:**
  * Plan 5's nine "Needed from plan 3" items are mapped in "Contract for plan 5"; eight are provided here with tests.
  * Plan 5 must own `write_secret_result`, and plan 4 should switch `annotation_resolve` to the kind registry. Neither of those plans is edited here beyond their migration numbers.
  * Plan 5 imports `CHUNK_BYTES`/`MCP_UPLOAD_MAX_BODY` from `collab_bridge_files` and extends the Task 8b gate through its registry. Its nginx `/mcp` value is already the same 768k; the `/mcp` rate zone of Task 8b applies to its tools too.
* **Name and type consistency:**
  * Classes: `CollabGrantStore` (incl. `revoke_write_grants`, `revoke_all_grants`, `record_received(…, request_sha256, bytes)`, `decided_for_key(actor, kind, key, outcomes)`, `bytes_last_24h(agent_id, *, exclude_ticket)`, `verified_upload(owner_key, …)`, `record_verified_upload(…, charged)`), `CollabMirror(root, *, rustfs, limits)` (per-artifact lock; `sync` does not publish grants), `CollabIntentApplier(outbox, mirror, *, rustfs, limits, spool_dir, lineage)` (incl. `register`, `register_staging_owner`, `sweep_staging`, `sweep_results`), `IntentContext` (incl. `assemble(…, charge)`, `extras`), `CollabArtifactBridge` (incl. `available`, `stage_chunk`, `stage_chunk_for`, `append_intent`, `read_result`, `limits`), `CollabBridgeLoop` (grants, mirror and applier tasks), `ArtifactMergeConflict.attempt_id`, `merge_commit(…, ticket_id)`.
  * Contract: `BridgeLimits` (incl. `mirror_max_manifests`), `validate_intent(…, limits)`, `request_digest`, `validate_changes(…, limits)`, `validate_chunk -> Chunk`, `upload_id_for`, `staging_path`, `staging_parts`, `StagedUpload` (`owner_key`), `upload_complete(root, owner_key, upload_id)`, `Actor`, `agent_owner_key`, `register_intent_kind`, `Intent` (`kind, actor, artifact_id, idempotency_key, submitted_at, payload, request_sha256`), `CommitPayload` (`inline_bytes`), `Change` (`name, op, text, upload_id`), `write_marker_beneath`, `unlink_beneath`, `remove_tree_beneath`, `register_large_body_admission`, `collab_waiters.waiter_slot`, `collab_lineage_poster_from_section`, `_poster_settings`, `_collab_grant_wiring`.
  * Ledger columns: `artifact_collab_intents.kind`/`actor_kind`/`actor_id`/`request_sha256` replace `collaborator_agent_id`, and `artifact_collab_uploads.owner_key`/`charged` replace `agent_id`.
  * Constants and patterns: `CHUNK_BYTES`, `MCP_UPLOAD_MAX_BODY`, `TICKET_RE`, `UPLOAD_RE`, `INTENT_MAX_AGE`, `FEED_MAX_AGE`, `MAX_WAITERS` (in `collab_waiters`).
  * Tables: `artifact_collab_intents`, `artifact_collab_invite_grants`, `artifact_collab_uploads`; migration `0045_artifact_collab_bridge.sql`.
  * Event types: `artifact_tool_used`, `artifact_commit_submitted`, `artifact_upload_staged` (denials use `audit_denied`).
  * Config: `control_plane.collab_artifacts` (worker), `collab_artifacts.enabled` in the render-collab slice (collab-public).
  * Removed: `MAX_COMMIT_TEXT_BYTES`, `MAX_INTENT_FILE_BYTES`, `MAX_MCP_COMMIT_BODY`, the 7 MiB `/mcp` body and the 64 MiB outbox-bytes cap; the hard-coded `max_manifests=200`; the `artifact_tool_denied` event type; the proposed `listCollabAgents`/`createExternalInvite`/`listCollabRooms` client methods (reuse `CollabApi`); a hand-rolled `difflib` diff (reuse `ArtifactBundle.diff`). Intents are now ≤ 64 KiB and bounded by `max_pending_intents_per_agent`, and staging is bounded by the staging caps.
