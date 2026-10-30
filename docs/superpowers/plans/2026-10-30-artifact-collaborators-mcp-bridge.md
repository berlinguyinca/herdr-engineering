# Artifact collaborators over the collab MCP bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 3 of the artifact sharing / collaboration design. The owner grants collab agents an artifact; those agents read it and commit to it through seven new tools on the collaboration MCP server. The spec says six. The seventh, `artifact_stage_chunk`, is an operator-approved deviation; see Decisions. The Share panel gains Collaborators and Activity tabs.

**Architecture:** `collab-public` has no database or registry, so the bridge is three directories:
* **Mirror** (`grants.json` with the published `limits`, `artifacts/<id>/head.json`, content-addressed `blobs/<sha256>`, `results/<ticket>.json`). Only the worker writes it, and it is mounted read-only into `collab-public`.
* **Outbox** (`intents/<ticket>.json`, `agents.json`, `staging/<agent_id>/<upload_id>/`). Only `collab-public` writes it, and the worker consumes it. The worker deletes consumed and swept staging.

File content of any size or type travels as hash-checked chunks staged in the outbox. The worker assembles them and verifies them against the declared sha256 and size, streaming them into RustFS. A commit then references the staged upload by `upload_id`. Small text can still go inline in the commit.

A new worker loop, `CollabBridgeLoop`, runs on a ~1 s tick and re-checks everything against Postgres. It runs as two asyncio tasks with their own asyncpg connections:
* a publisher (grants, feed, mirror, sweeps);
* an applier (intents) with per-agent lanes, so that one large assembly never stalls the `grants.json` heartbeat or other agents.

The applier checks:
* the agent, from `agents.json`;
* the grant, from `artifact_acl` rows for `collab:<agent_id>`;
* retraction, from the durable state and a worker-local gate.

It assembles and stores staged uploads and stores inline text. It then calls the already-built `ArtifactStore.merge_commit` (in no outer transaction), records the outcome, writes the result file and refreshes the mirror. Owner routes on the dashboard manage grants and serve the Activity data. Grants are DB-only and never enter the web process's in-memory ACL.

**Tech Stack:** Python 3.12 / asyncpg (control plane, worker), stdlib file I/O (`os.open` + `O_NOFOLLOW`, rename-into-place, `os.link` for no-replace), plain ES-module JS (`packages/`), node:test, Playwright, Postgres migrations in `deploy/control-plane/migrations`, docker compose overlays and nginx in `deploy/control-plane`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md`:
* section B (including the merge rules, scope and limits; the limits are superseded as recorded under Decisions);
* section C;
* "Builder-check resolutions" and "Second builder check" (authority split, `collab:<agent_id>` ACL rows, 60 s intent age, `agents.json`, mirror = head + previous 5 versions' text files plus all manifests).

The merge engine (plan 2) is in `herdr_engineering/control_plane/artifact_merge.py` and `ArtifactStore.merge_commit` (`artifacts.py`). Its precedents:
* share-ingest: `artifact_shares.ShareStore.ingest_and_rotate_events`, `share_public.EventsWriter` / `_open_beneath`;
* MCP: `herdr_engineering/control_plane/collab_mcp.py`;
* verified streaming into the store: `RustFSClient.put_stream(expected_sha256, expected_size)` as used by `artifact_uploads.complete_upload`.

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
   * **Restrict all access is optional and operator-driven, never automatic.** When the retraction fails to persist, the response offers "restrict all access now?". The operator can accept through `herdr-eng artifacts retract --restrict-all`, the **Restrict all access** button in the Collaborators tab, or `POST /api/v1/artifacts/{id}/collab-restrict`. That action also revokes the read collaborator grants, so the mirror drops the artifact and collaborators no longer see it. Without that action, read collaborators keep seeing the artifact until the operator retries the retraction.
8. **Hosting.** The MCP endpoint stays on its existing host and path. The member/viewer UI on `artifacts.metabolomics.us` belongs to plans 4 and 5. Nothing in this plan changes for it.

## Global Constraints

* **Principal and identity**
  * A collaborator's principal is exactly `collab:<agent_id>`. It is used as `created_by` for `merge_commit`, and the idempotency namespace follows from it. `collaborator_agent_id=<agent_id>`.
  * Grants are `artifact_acl` rows for `collab:<agent_id>`. Permissions are `["read"]` (read) or `["read","edit"]` (write), and `expires_at` is required. The default is 30 days and the maximum is 90.
  * Internal and external agents may both hold write grants.
  * A pending invite grant is never an `artifact_acl` row.
* **Tools and the MCP endpoint**
  * The seven tools are `artifacts_list`, `artifact_head`, `artifact_read`, `artifact_history`, `artifact_stage_chunk`, `artifact_commit` and `artifact_commit_result`.
  * Every tool is validated by hand (`additionalProperties: False` in the schema and an unknown-argument check in code).
  * Results that carry artifact content, history text or conflict text are wrapped `untrusted=True`.
  * `tools_for` offers the tools only to an agent with at least one live grant in `grants.json`.
  * Body caps:
    * `MAX_BODY` (64 KiB) is unchanged for every tool and route.
    * `MCP_UPLOAD_MAX_BODY = 786_432` (768 KiB) applies to `artifact_stage_chunk` only, and only for an authenticated agent holding a write grant.
* **Commits and staging**
  * A commit change is either:
    * `{name, op: "put", text}` with inline UTF-8 text, bounded in practice by the 64 KiB body;
    * `{name, op: "put", upload_id}`, which references a complete staged upload of any size or type under the **caller's own** staging dir;
    * `{name, op: "delete"}`.
  * Chunks are `CHUNK_BYTES = 524_288` (raw), sent base64 with their own sha256. Every chunk except the last is exactly `CHUNK_BYTES`.
  * An upload declares `name`, `size_bytes` (≥ 1; an empty file is inline `text: ""`) and the whole-file `file_sha256` on every chunk, and `total` must equal `ceil(size_bytes / CHUNK_BYTES)`.
  * `upload_id` is derived, never chosen: `"upl_" + sha256(agent_id | artifact_id | name | file_sha256 | size_bytes)[:24]`. Re-sending the same file therefore resumes the same upload.
  * A chunk is written to a temp file and then `os.link`ed to its final name, so its final name never holds partial content and an existing chunk is never replaced. The same index with the same sha replays as success. The same index with a different sha is refused with 409.
  * Write grants are for plain file artifacts only: not `artifact_type == "report"`, no `report_descriptor` and no `source_manifest_blob` on the head. Read grants are allowed for any artifact.
  * Quota accounting:
    * The quota counts inline text bytes, from the ledger, plus each staged upload's size **once**, when the worker first verifies it (`artifact_collab_uploads`).
    * The check runs **before** assembly, using the declared sizes of not-yet-verified uploads.
    * A re-commit of an already verified upload, for example after a conflict, costs nothing more and is never re-assembled.
* **Intent validation in the worker**
  * The worker rejects an intent older than 60 s at first receipt (`submitted_at`).
  * It fails closed when `agents.json` is older than 120 s, and `collab-public` fails closed when `grants.json` (and so `limits`) is older than 120 s or missing.
  * A change's allowed keys are `{name, op, text, upload_id}`. Any `sha256`, `storage_key`, `blob_sha256`, `entry`, `size_bytes` or `media_type` key in a change is rejected. Content identity comes only from inline text or from bytes the worker assembled and verified itself, never from a caller-supplied hash or key.
  * Every name passes `share_bundle.normalise_name` on both sides. A staged upload's declared `name` and `artifact_id` must equal the change's name and the intent's artifact.
* **Retraction**
  * A commit is refused if any of these holds:
    * the artifact's `lifecycle == 'retracted'` (checked inside `merge_commit`);
    * the base or head version has `state = 'retracted'`;
    * a worker-local `ArtifactGraphGate`, loaded with `load_retractions()` at least every 30 s, says the artifact, base or head is retracted.
  * The mirror never contains a retracted artifact or version.
  * When a web-side retraction fails to persist, the retract route revokes every `collab:` write grant on that artifact (Decision 7).
  * Restricting all access (read grants too, hiding the artifact from collaborators) is a separate operator action, never automatic (Decision 7, Task 10).
  * The residual is that the revoke itself can fail. If it fails because Postgres is unreachable, the worker's `merge_commit` fails too. The route reports a failed revoke and never claims success.
* **`merge_commit` contract**
  * Always called without an outer transaction, so its `artifact_collab_attempts` row persists. It is called with `max_text_bytes = max_text_merge_bytes`.
  * `ArtifactMergeConflict` gives result `conflict`. It carries `head_version_id`, `{name, kind}` per file and `merged_text`, which is non-null only for `modify_modify_text`. All `merged_text` in one result is capped at `max_conflict_text_bytes`; over the cap a file gets `merged_text: null, truncated: true`.
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
  * Every commit submission and every denial goes through the collab event log.
  * Allowed reads are coalesced: at most one per (agent, artifact, tool) per 600 s, as `TERMINAL_AUDIT_EVERY` does.
  * Chunk staging is audited once per upload: an `artifact_upload_staged` event when the upload becomes complete, carrying upload id, artifact and size, never content.
  * The new event types join `BULK_EVENTS`.
* **Migrations.** Start at 0044 (0043 is this branch's). Re-check `git ls-tree origin/main deploy/control-plane/migrations/` right before committing and renumber if main has taken it. Migrations must be additive and idempotent.
* **Configuration and conventions**
  * Configuration is YAML only. No environment variables are read by the code. Everything lives under `control_plane.collab_artifacts`, which is off unless `enabled` is exactly `true`.
  * Compose paths come from `deploy.sh` exports, as the share dirs do.
  * `BridgeLimits.from_mapping` (Task 3) validates every limit: positive ints, and a config with `0`, a negative value or a bool is refused at load. Defaults:

    | Key | Default | Purpose |
    |---|---|---|
    | `daily_bytes_per_agent` | `100000000000` (100 GB) | quota |
    | `commits_per_minute` | `30` | anti-abuse |
    | `chunks_per_minute` | `1200` | anti-abuse (~10 MiB/s per agent) |
    | `max_changes_per_commit` | `100` | sanity |
    | `max_pending_intents_per_agent` | `4` | sanity |
    | `max_file_bytes` | `100000000000` | per staged file; sanity, implied by the quota |
    | `staging_ttl_seconds` | `86400` | sweeper |
    | `staging_bytes_per_agent` | `200000000000` | sanity, reserved by declared size |
    | `staging_bytes_total` | `2000000000000` | sanity, whole volume |
    | `max_text_merge_bytes` | `2097152` | three-way text merge only |
    | `max_conflict_text_bytes` | `8388608` | per result |
    | `mirror_history_versions` | `5` | mirror |
    | `mirror_max_file_bytes` | `1073741824` | per mirrored text file |
    | `mirror_max_artifact_bytes` | `17179869184` | per mirrored artifact |
    | `apply_concurrency` | `4` | applier lanes |

  * The worker publishes the effective limits in `grants.json` (`limits`), so that `collab-public` enforces the same values without config of its own or new flags. The worker re-enforces every limit from its own config.
  * The outbox volume (staging) and the worker's spool dir must sit on large storage. `deploy.sh` places them beside the share dirs, and `COLLAB.md` says so.
  * Web: everything goes through `esc()`, with no inline handlers and no tokens in storage. Owner POSTs are same-origin. Permission denials are non-disclosing 404s.
* **Commits.** Every commit ends with the attribution trailer the executing session's system reminder prescribes.

## Review Focus

* **Revocation mid-flight.** If the owner revokes a grant while an intent sits in the outbox (or the worker is mid-tick), the next tick must reject it (`rejected`, "no write grant"). Within ~2 s of the revoke, `artifacts_list` must stop listing the artifact. The revoked agent's staging for that artifact must be swept. Test all three, in Task 5 and Task 9 and in Task 8's end-to-end test.
* **Hostile names and text.** Commits containing any of the following must never escape the mirror or staging, crash the applier or let a mirror read return a file outside `blobs/`:
  * the names `../x` and `a/../../b`, a NUL or a CR in a name, and a name that is a directory prefix of another file;
  * 100 000 one-byte lines, and a very large single line.

  Pin with Task 3 (names, staging paths) and Task 5 (applier).
* **Forged or tampered staging.** None of the following may ever produce a version or reference another agent's or artifact's bytes:
  * an `upload_id` copied from another agent;
  * an upload staged for artifact X and committed to Y;
  * a part replaced on disk after staging;
  * a missing or symlinked part;
  * a declared size or sha256 that does not match.

  Each is refused with the same "not found", or with "upload failed verification". Pin in Task 3, Task 5 and Task 7.
* **Crash and retry.** A worker crash between `merge_commit` and the result write, or an agent resending the same `idempotency_key` with a new ticket, must yield exactly one version, and both tickets must report it. This must hold even when the restart comes more than 60 s after receipt (the `received` ledger row exempts it from the age check), and even when the staged upload was already consumed. Pin in Task 5.
* **Large uploads do not stall the bridge.** While a multi-GB assembly runs, `grants.json` keeps its heartbeat and other agents' intents keep being applied. Pin in Task 9.
* **One agent cannot see another's results.** Agent B guessing or copying agent A's ticket gets the same "not found" as an unknown ticket, including during the long-poll. Pin in Task 7.
* **Share panel tabs under failure.** If the collaborators route returns 503 (the bridge is disabled) or the agent list cannot be fetched (the collab overlay is off), the Links tab must keep working, and the Collaborators tab must say why without a broken form. Pin in Task 12.

---

### Task 1: Migration 0044 and the collaborator store

**Files:** Create `deploy/control-plane/migrations/0044_artifact_collab_bridge.sql`, `herdr_engineering/control_plane/artifact_collab_store.py`; Test `tests/control_plane/test_artifact_collab_store.py`. The test reuses the isolated Postgres fixture pattern of `test_artifact_version_parents.py`, applying migrations through 0044.

**Interfaces:**
* Migration:
  * `artifact_collab_intents(ticket_id text PK, kind text NOT NULL DEFAULT 'commit', actor_kind text NOT NULL CHECK (actor_kind IN ('agent','user')), actor_id text NOT NULL, artifact_id text, base_version_id text, idempotency_key text, bytes bigint NOT NULL DEFAULT 0, outcome text NOT NULL CHECK (outcome ~ '^[a-z_]{1,32}$'), version_id text, attempt_id text, reason text, received_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz)`.
    * `received` means undecided. The `commit` kind's outcomes are `created`, `merged`, `conflict` and `rejected`; other kinds' handlers choose their own.
    * No FK on `artifact_id`: an intent can name an artifact that does not exist, and a non-commit kind may name none.
    * Indexes on `(artifact_id, received_at DESC)` and `(actor_kind, actor_id, received_at DESC)`.
    * `bytes` is inline text bytes only. Staged bytes are counted in `artifact_collab_uploads`.
    * Index on `(actor_kind, actor_id, kind, idempotency_key)`, for the replay short-circuit.
  * `artifact_collab_uploads(owner_key text NOT NULL, upload_id text NOT NULL, artifact_id text NOT NULL, name text NOT NULL, sha256 text NOT NULL, size_bytes bigint NOT NULL, storage_key text NOT NULL, media_type text NOT NULL, charged boolean NOT NULL, verified_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (owner_key, upload_id))`.
    * Only the worker writes it, after `put_stream` verified the bytes. It is the sole authority that an upload is usable.
    * `owner_key` is the staging owner: the agent id for collaborators (plan 5 adds `m_…` member keys).
    * `charged` is true only for uploads counted against the per-agent daily quota, which means `commit` uploads.
  * `artifact_collab_invite_grants(invite_id text PK, artifact_id text NOT NULL REFERENCES artifacts, permission text NOT NULL CHECK (permission IN ('read','write')), expires_at timestamptz NOT NULL, granted_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), resolved_agent_id text, resolved_at timestamptz)`.
  * `ALTER TABLE artifact_collab_attempts ADD COLUMN IF NOT EXISTS ticket_id text`.
  * Include a `-- DOWN:` comment as 0043 does.
* `class CollabGrantStore(conn)`. Every method is `async`:
  * Grants:
    * `grant(artifact_id, agent_id, permission: Literal['read','write'], *, expires_at: datetime, granted_by: str) -> dict` upserts `artifact_acl` for `collab:<agent_id>`. An `expires_at` more than 90 days out raises `ValueError`.
    * `revoke(artifact_id, agent_id) -> bool`.
    * `revoke_write_grants(artifact_id) -> int` deletes every `collab:` row on the artifact whose permissions include `edit` and returns the count. It is used by the retraction fail-safe (Task 10).
    * `revoke_all_grants(artifact_id) -> int` deletes every `collab:` row on the artifact, read and write, and returns the count. It is used only by the operator's restrict-all action (Task 10), never automatically.
    * `live_grants() -> list[dict]` returns `{artifact_id, agent_id, permission, expires_at}` for unexpired `collab:` rows.
    * `grant_for(artifact_id, agent_id) -> dict | None`.
  * Invite grants:
    * `add_invite_grant(invite_id, artifact_id, permission, *, expires_at, granted_by)`.
    * `resolve_invite_grants(join_to_agent: dict[str, dict]) -> int` creates the real grant with `expires_at = min(row.expires_at, agent.hard_expiry)` and marks the row resolved.
    * `pending_invite_grants(artifact_id) -> list[dict]`.
  * Intent ledger:
    * `record_received(ticket_id, kind, actor: Actor, artifact_id, base_version_id, idempotency_key, bytes) -> dict | None` returns the existing row if the ticket is known.
    * `record_outcome(ticket_id, outcome, *, version_id=None, attempt_id=None, reason=None)`.
    * `decided_for_key(actor, kind, idempotency_key, outcomes: frozenset[str]) -> dict | None` returns the row whose outcome is in `outcomes`.
    * `bytes_last_24h(agent_id) -> int` is the sum over the last 24 h of two things:
      * ledger `bytes` (kind `commit`, agent actor, outcomes other than `rejected`);
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
  * `record_received` twice returns the first row.
  * `bytes_last_24h` sums only the last 24 h and excludes `rejected` ledger rows. A 5 GB verified upload counts once, even when two tickets reference it.
  * `record_verified_upload` twice keeps one row.
  * A `charged: false` upload of 5 GB under owner key `m_x` and one under the agent's own key both leave `bytes_last_24h(agent)` unchanged.
  * `decided_for_key` ignores rows whose outcome is outside `outcomes`, and rows of another kind or actor.
  * A `user` actor row with kind `x.test` and outcome `stored` is accepted by the CHECK constraint.
  * `resolve_invite_grants` caps `expires_at` at `hard_expiry` and is idempotent.
  * `collaborators` counts commits and conflicts per agent.
  * `attempts` includes conflict names.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_collab_store.py -q` and expect FAIL.
- [ ] **Step 3: Implement** the migration and the store. **Step 4: Run** and expect PASS, with `tests/control_plane/test_artifact_merge_commit.py` still passing.
- [ ] **Step 5:** Re-check origin/main's migration numbers. **Commit** `feat(artifacts): collaborator grants and intent ledger (migration 0044)`.

### Task 2: Keep `collab:` principals out of the web ACL

**Files:** Modify `herdr_engineering/control_plane/artifact_acl.py`, `herdr_engineering/control_plane/artifacts.py` (`load_acl`), `herdr_engineering/control_plane/api.py` (the `/acl` and `/acl/revoke` routes near line 3589); Test `tests/control_plane/test_artifact_acl.py` (extend).

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

**Files:** Create `herdr_engineering/control_plane/collab_bridge_files.py` (imported by both sides; stdlib only, no asyncpg); Test `tests/control_plane/test_collab_bridge_files.py`.

**Interfaces:**
* Protocol constants. These are not configuration; plan 5 imports them:
  * Sizes: `CHUNK_BYTES = 524_288`, `MCP_UPLOAD_MAX_BODY = 786_432`, `MAX_MESSAGE = 1000`.
  * Ages: `INTENT_MAX_AGE = 60.0`, `FEED_MAX_AGE = 120.0`.
  * Patterns: `TICKET_RE = r"ict_[0-9a-f]{24}\Z"`, `UPLOAD_RE = r"upl_[0-9a-f]{24}\Z"`, `SHA_RE = r"[0-9a-f]{64}\Z"`.
  * `MAX_CHUNK_INDEX = 9_999_999`. Part files are named `{index:07d}`.
* Limits:
  * `@dataclass(frozen=True) class BridgeLimits` holds every key of the Configuration table, with those defaults.
  * `BridgeLimits.from_mapping(d: dict) -> BridgeLimits` refuses unknown keys, and refuses `0`, negatives and bools.
  * `to_dict()` serializes it for `grants.json`.
* Safe I/O:
  * `read_json_beneath(root: Path, parts: list[str], *, max_bytes: int) -> dict | None`. It opens each component with `O_NOFOLLOW` through `dir_fd` (the `share_public._open_beneath` approach; extract and share it rather than copy if that stays small). It accepts regular files only, enforces the size cap before parsing and returns None on any failure.
  * `read_bytes_beneath(...) -> bytes | None` is the same check for blobs and parts.
  * `open_beneath(root, parts) -> BinaryIO | None` is the streaming variant used by assembly.
  * `atomic_write_json(dir: Path, name: str, obj: dict, *, mode=0o644)` serializes with `ensure_ascii=False`, writes `.tmp-<rand>`, fsyncs and `os.replace`s. It uses the share-bundle modes (files 0644, dirs 0755), so the reader container's uid can read what the other container's uid wrote.
  * `atomic_write_bytes(dir, name, data)` does the same for bytes.
  * `link_new_bytes(dir, name, data, *, mode=0o600) -> bool`. It writes `.tmp-<rand>`, fsyncs and `os.link`s it to `name`, then removes the tmp. It returns False if `name` already exists and never replaces it.
* Staging:
  * `@dataclass(frozen=True) class Actor: kind: Literal['agent','user']; id: str`.
  * `agent_owner_key(agent_id) -> str` returns the agent id. It refuses an id starting with `m_`, which is reserved for plan 5's member owner keys; agent ids come from `ids.new_id("agent")` and never have that prefix.
  * `upload_id_for(owner_key, artifact_id, name, file_sha256, size_bytes) -> str`.
  * `staging_path(root, owner_key, upload_id) -> Path`. The `owner_key` comes from the authenticated principal or the validated intent, never from a caller argument.
  * `@dataclass(frozen=True) class StagedUpload: upload_id: str; agent_id: str; artifact_id: str; name: str; size_bytes: int; file_sha256: str; total: int; created_at: float`. It is persisted as `meta.json` in the upload dir.
  * `@dataclass(frozen=True) class Chunk: artifact_id: str; name: str; size_bytes: int; file_sha256: str; total: int; index: int; data: bytes`.
  * `validate_chunk(args: dict, limits: BridgeLimits) -> Chunk`. It requires:
    * strict base64;
    * the chunk's own `sha256` matches the decoded data;
    * `0 ≤ index < total`;
    * `total == ceil(size_bytes / CHUNK_BYTES)`;
    * `len(data) == CHUNK_BYTES` except for the last chunk, which is the exact remainder;
    * `1 ≤ size_bytes ≤ limits.max_file_bytes`;
    * the name passes `normalise_name`/`check_names`.
  * `upload_complete(root, agent_id, upload_id) -> bool` holds when the indices `0..total-1` are all present, or when the worker's `verified` marker is present. The marker is a hint for collab-public only; the worker never trusts it.
  * `list_parts(...)` returns the present indices.
* Intent kinds:
  * `register_intent_kind(kind: str, validate_payload: Callable[[dict, BridgeLimits], object], *, actors: frozenset[str]) -> None`. `kind` must match `^[a-z][a-z0-9_.]{0,47}\Z`. Registering a kind twice raises. `actors` is the set of actor kinds allowed to send it.
  * Plan 3 registers only `commit` (`validate_commit_payload`, `actors={"agent"}`), at import of `collab_bridge_files`. Plan 4 (`annotation_resolve`) and plan 5 (`member.*`) register theirs from their own modules.
* Validation:
  * `new_ticket() -> str`.
  * `validate_intent(raw: dict, *, filename: str, limits: BridgeLimits) -> Intent` raises `IntentError(reason)`.
    * The envelope is `{ticket, kind, actor, artifact_id?, idempotency_key?, submitted_at, payload}`.
    * An unregistered kind gives `IntentError("unknown intent kind")`, and so does an actor kind not allowed for the kind.
  * `@dataclass(frozen=True) class Intent: ticket: str; kind: str; actor: Actor; artifact_id: str | None; idempotency_key: str | None; submitted_at: float; payload: object`.
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
  * **Safe I/O:**
    * A symlinked intent file, a symlinked parent dir, a FIFO and an oversize file all return None.
    * A symlinked part makes `open_beneath` return None.
    * `atomic_write_json` never leaves a partial target. Simulate this by raising inside write: the target is unchanged and the tmp file is removed.
    * `link_new_bytes` on an existing name returns False and leaves the original bytes.
  * **Intents and changes:**
    * A filename and ticket mismatch raises, as do `kind: "share_create"` and an unknown top-level key.
    * A `commit` from a `user` actor raises.
    * A test-registered kind `x.test` (with `actors={"user"}`) validates through its own payload validator, and a second registration of it raises. The test registry is reset afterwards.
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
    * `upload_id_for` is deterministic and differs per agent.
  * **Limits:** `BridgeLimits.from_mapping({"daily_bytes_per_agent": 0})` raises, as does an unknown key and a bool value. The defaults equal the Configuration table.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS, along with `tests/control_plane/test_share_public.py` if `_open_beneath` was extracted.
- [ ] **Step 5: Commit** `feat(collab): artifact bridge file contract and chunk staging layout`.

### Task 4: Mirror publisher (worker side)

**Files:** Create `herdr_engineering/control_plane/collab_mirror.py`; Test `tests/control_plane/test_collab_mirror.py` (Postgres fixture and a fake RustFS keyed by sha256).

**Interfaces:**
* Consumes `CollabGrantStore.live_grants` (Task 1), `ArtifactStore.list_versions`/`get_version`, `ArtifactGraphGate.load_retractions`, and `BridgeLimits`/`atomic_write_*` (Task 3).
* Produces `class CollabMirror(root: Path, *, limits: BridgeLimits, max_manifests: int = 200, clock=time.time)`. History depth, per-file and per-artifact caps come from `limits.mirror_history_versions`, `mirror_max_file_bytes` and `mirror_max_artifact_bytes`. Its methods:
  * `async publish_grants(grants, store, *, heartbeat: float = 30.0) -> bool`:
    * It writes `grants.json = {generated_at, limits: limits.to_dict(), agents: {agent_id: {artifact_id: {permission, expires_at}}}}`.
    * It writes at once when the content changed, and otherwise at least every `heartbeat` seconds, refreshing `generated_at` so the 120 s freshness check in collab-public never trips while the worker is alive.
    * It returns whether it wrote.
  * `async refresh(store, artifact_id, *, gate) -> dict`. It writes `artifacts/<artifact_id>/blobs/<sha256>` for the text files (UTF-8 decodable, ≤ `mirror_max_file_bytes`) of the head and the previous `mirror_history_versions` non-retracted versions. It streams each blob from RustFS to the file, skips blobs that exist, and stops adding blobs once the artifact reaches `mirror_max_artifact_bytes`; the files left out get `mirrored: false`. It then writes `head.json` **last**:
    * `{artifact_id, title, artifact_type, writable: bool, writable_reason, head: {version_id, version_number}, versions: [{version_id, version_number, parent_version_ids, created_via, collaborator_agent_id, author_label, message, created_at, merge: bool, mirrored: bool, files: [{name, size_bytes, sha256, media_type, text: bool, mirrored: bool}]}]}`, newest first and capped at `max_manifests`.
    * `author_label` is `"owner"` or `"collaborator <agent_id>"` and never an email. `message` is `provenance.message`, capped at 1000 characters.
    * Unreferenced blobs are deleted after the swap.
  * `async remove(artifact_id)` deletes the artifact's mirror.
  * `async sync(store, *, gate, force: set[str] = frozenset())` publishes grants, refreshes every granted artifact whose head changed (or that is in `force`), and removes artifacts that have no live grant or are retracted.

- [ ] **Step 1: Write failing tests:**
  * **Content:**
    * A granted plain artifact gets `head.json` and blobs whose sha256 matches the content.
    * A binary or non-UTF-8 file is listed with `text: false` and has no blob.
    * A file over `mirror_max_file_bytes` (set small in the test) is listed with `mirrored: false` and gets no blob.
    * With `mirror_history_versions: 5`, the 7th-newest version has `mirrored: false` and no blobs.
    * A report artifact has `writable: false, writable_reason: "report artifacts are read-only for collaborators"`.
  * **Retraction and revocation:**
    * A retracted version is absent from `versions`, and a retracted artifact's directory is removed.
    * Revoking the last grant removes the directory on the next `sync`.
  * **`grants.json`:**
    * It carries `limits` equal to the configured `BridgeLimits`.
    * It is rewritten immediately on a grant change. When unchanged, it is not rewritten within 30 s but is rewritten (newer `generated_at`) after 30 s.
  * **Safety:**
    * A crash simulated mid-refresh leaves the previous `head.json` intact.
    * No file in the mirror contains a `created_by` email: assert against a seeded `owner@example.org`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): read-only collaborator mirror`.

### Task 5: Intent applier and staging assembly (worker side)

**Files:** Create `herdr_engineering/control_plane/collab_intents.py`; Modify `herdr_engineering/control_plane/rustfs.py` (an optional `spool_dir` keyword on `put_stream`, passed to `NamedTemporaryFile(dir=…)`; the default is unchanged); Test `tests/control_plane/test_collab_intents.py` (Postgres fixture, fake RustFS, temp outbox and mirror), plus a `spool_dir` case in the existing RustFS tests.

**Interfaces:**
* Consumes:
  * Task 1's store;
  * Task 3's `validate_intent`, `read_json_beneath`, `open_beneath`, `StagedUpload`, `upload_complete` and `BridgeLimits`;
  * Task 4's `CollabMirror.refresh`;
  * `ArtifactStore.merge_commit`/`register_report_blob`/`get_version`/`load_retractions`;
  * `RustFSClient.put_stream`.
* Produces `class CollabIntentApplier(outbox: Path, mirror: CollabMirror, *, rustfs, limits: BridgeLimits, spool_dir: Path, lineage=None, context_extras: Mapping[str, object] = {}, clock=time.time, max_per_tick: int = 20)` with these methods:
  * `register(kind, handler, *, lane_key=None, replay_outcomes: frozenset[str] = frozenset())`. Plan 3 registers `commit` with `replay_outcomes={"created","merged"}`.
    * `handler` is `async (ctx: IntentContext, intent: Intent) -> dict`. It returns `{result, version_id?, reason?, …}`, and the applier records and writes that.
    * `lane_key` is `(intent, agent_row | None) -> str`. The default is `f"{actor.kind}:{actor.id}"`. Plan 5 maps every actor of one member to that member's lane.
  * `@dataclass class IntentContext: actor: Actor; agent: dict | None; conn; store: ArtifactStore; grants: CollabGrantStore; limits: BridgeLimits; assemble; extras: Mapping[str, object]`.
    * `agent` is the actor's `agents.json` row, or None for user actors.
    * `assemble(owner_key, upload_id, *, artifact_id, name, charge: bool) -> entry` is step 10's staged-upload path as a callable: reuse a verified upload, else assemble, verify, `register_report_blob` and `record_verified_upload(charged=charge)`.
    * `extras` is `context_extras`, passed through untouched. Plan 5 supplies `share_store`/`server` there, so plan 3 never imports them.
  * `async tick(acquire) -> dict` returns `{applied, conflicts, rejected, skipped}`.
    * `acquire` is an async context manager that yields a connection from the applier's own pool.
    * It runs up to `limits.apply_concurrency` intents concurrently, and never two with the same lane key at once. Within a lane, intents run oldest first.
    * A lane that is still busy from the previous tick is skipped, not awaited.
  * `register_staging_owner(prefix: str, keep: Callable[[str, StagedUpload], Awaitable[bool]])`. Owner dirs whose key starts with `prefix` are exempt from the agent and grant rules of `sweep_staging`. They are deleted only past the TTL or when `keep` returns False. Plan 3 registers no prefix.
  * `async sweep_staging(conn, *, agents: dict, grants: list[dict]) -> int` deletes upload dirs in these cases:
    * the dir is older than `staging_ttl_seconds` by its newest part mtime;
    * the agent is not `active` in `agents.json`;
    * the agent has no live write grant on the upload's `artifact_id`;
    * a `meta.json` is missing or invalid and the dir is older than 60 s. The grace age avoids racing collab-public between creating the dir and linking `meta.json`.

    The agent and grant rules apply only to agent owner dirs. A dir under a registered prefix follows its `keep` predicate instead.

    It never deletes an upload referenced by an intent file still in the outbox, and it returns the count.
  * `sweep_results(max_age=3600)`.
* Per intent file (names matching `TICKET_RE + ".json"`, regular files only). Steps 1–3, 5 and 6 are generic and run for every kind. Steps 4 and 7–12 are the `commit` handler.
  1. If the ledger has the ticket with a decided outcome, rewrite the result file from the row and unlink the intent.
  2. Validate the intent; an invalid one, or an unregistered kind, gets `rejected` with a fixed reason and no echo of content.
  3. If `decided_for_key(actor, kind, intent.idempotency_key or ticket, replay_outcomes)` finds a row, record this ticket with the same outcome and version, write its result and unlink it. This happens before any upload lookup, so a retry after the upload was consumed still succeeds.
  4. Resolve each staged put (`commit` handler):
     * A `verified_upload(agent, upload_id)` row with the same `artifact_id` and `name` is reused as is, with no staging read and no quota charge.
     * Otherwise read `meta.json` from `staging_path(outbox, agent_owner_key(intent.actor.id), upload_id)`. Missing gives `rejected` "upload not found", and so does a different `artifact_id` or `name`.
     * This path is derived from the intent's agent, so another agent's `upload_id` is always "not found".
  5. If there is no ledger row and `now - submitted_at > INTENT_MAX_AGE`, reject `expired`. Otherwise `record_received(…, bytes=inline_bytes)`, where `inline_bytes` is 0 for kinds other than `commit`.
  6. For an agent actor, `agents.json` (`read_json_beneath`, ≤ 4 MiB) must be fresh and must show the agent `active` with `expires_at > now`. A user actor skips this step; its kind's handler authenticates it (plan 5: against its member directory). The applier then computes the lane and dispatches to the kind's handler.
  7. `grant_for` must show `write`, unexpired.
  8. Check that the artifact is writable, and the gate plus version `state` for the base and the current head.
  9. Apply the daily quota: `bytes_last_24h(agent) + inline_bytes + Σ declared size_bytes of unverified uploads ≤ daily_bytes_per_agent`. This check runs before any assembly or RustFS write.
  10. Store the content:
     * An inline put's text is stored with `rustfs.put` and `register_report_blob(artifact_id, …)`.
     * A staged put goes through `ctx.assemble(agent_owner_key(agent), upload_id, artifact_id=…, name=…, charge=True)`. An unverified staged put requires all its parts, else `rejected` "upload incomplete".
       * Its parts are streamed in index order through `open_beneath` (bounded memory) into `asyncio.to_thread(rustfs.put_stream, reader, expected_sha256=meta.file_sha256, expected_size=meta.size_bytes, media_type=…, spool_dir=spool_dir)`.
       * Then come `register_report_blob` and `record_verified_upload`.
       * The parts are then deleted, leaving only `meta.json` and a `verified` marker until the TTL, so collab-public's precheck still passes for a re-commit.
       * An `IntegrityError` gives `rejected` "upload failed verification", and the whole upload dir is deleted.
     * The entry is `{name, media_type, sha256, size_bytes, storage_key}`. `media_type` is the head entry's media type, else `mimetypes.guess_type`, else `"text/plain"` for inline text or `"application/octet-stream"` for staged bytes.
  11. Call `merge_commit(created_by=f"collab:{agent}", collaborator_agent_id=agent, idempotency_key=intent.idempotency_key or ticket, max_text_bytes=limits.max_text_merge_bytes, …)` with no outer transaction. Large and binary puts need no special case: a change on one side only is taken whole, and a change on both sides conflicts as `binary`/`oversize`.
  12. Finish the intent:
      * `record_outcome`.
      * Write `results/<ticket>.json = {ticket, kind, actor: {kind, id}, artifact_id, result, version_id?, version_number?, head_version_id?, merged_names?, conflicts?: [{name, kind, merged_text, truncated}], reason?, decided_at}`, applying the `max_conflict_text_bytes` cap.
      * Unlink the intent.
      * On success, call `mirror.refresh` and `lineage(version)` (Task 6). Their errors are logged and never fail the tick.
* Log lines carry only ticket, upload id, agent, artifact and outcome.

- [ ] **Step 1: Write failing tests:**
  * **Outcomes:**
    * A fast-forward gives `created` with parents `[head]`.
    * A stale base with disjoint edits gives `merged` with parents `[head, base]`.
    * Overlapping edits give `conflict` with markers in `merged_text`, an unchanged head, one `artifact_collab_attempts` row with `ticket_id` and the ledger row `conflict`. This proves the attempt row persisted (no outer transaction).
  * **Staged puts:**
    * A 3-chunk staged binary file (non-UTF-8) is assembled. It gives `created` with an entry whose sha256 and size equal the declared ones, and the staging dir is gone.
    * A staged text file over `max_text_merge_bytes` (set small) changed on one side only gives `merged`.
    * The same file changed on both sides gives `conflict` with kind `oversize` and `merged_text: null`.
    * A binary file changed on both sides gives `conflict` with kind `binary`.
    * Re-committing that upload with `base = head_version_id` gives `created`, with no second `put_stream` call and no second quota charge.
    * After verification, the upload dir holds only `meta.json` and the `verified` marker.
  * **Staging refusals:**
    * Each of these is `rejected` "upload not found", and in every case nothing is put to RustFS:
      * an `upload_id` that exists only under another agent's dir;
      * an upload whose `meta.artifact_id` is another artifact;
      * an upload whose `meta.name` differs from the change's name.
    * A missing part is `rejected` "upload incomplete".
    * A part whose bytes were overwritten on disk after staging is `rejected` "upload failed verification": no version, no `artifact_blobs` row and no `artifact_collab_uploads` row exist, and the upload is deleted.
    * A forged `verified` marker with no table row is ignored: the parts are assembled or the intent is rejected as incomplete.
    * A symlinked part is `rejected`.
    * A declared size that pushes the agent over `daily_bytes_per_agent` is `rejected` "daily quota exceeded" before any RustFS call: assert that the fake's put counters are 0.
  * **Refusals:**
    * No grant is `rejected` with "no write grant", and so is a read-only grant.
    * A grant revoked after the intent was written is `rejected`.
    * An expired grant is `rejected`.
    * An agent missing from `agents.json` or revoked there is `rejected`; a stale `agents.json` (> 120 s) is `rejected` with "agent feed stale".
    * An intent older than 60 s with no ledger row is `expired`.
  * **Crash and retry:**
    * An intent with a `received` row older than 60 s is still applied (crash recovery).
    * Re-running the same file after a simulated crash before unlink yields the same version, even though the staged upload was already deleted.
    * Two tickets with the same `idempotency_key` produce one version, and both results name it.
    * The same holds for a staged put whose parts were already deleted after the first ticket succeeded: the second ticket short-circuits on the key.
  * **Retraction and scope:**
    * An artifact retracted in the DB is `rejected`.
    * A retracted head version (`state`) is `rejected`.
    * A gate-only retraction loaded from `load_retractions` is `rejected`.
    * A report artifact is `rejected` with "not writable by collaboration".
  * **Forged input:**
    * A forged change with `storage_key` pointing at another artifact's scoped blob is `rejected` by validation, and that blob is never referenced.
    * `kind: "share_create"` is `rejected`, no share row exists and the share module is never imported (assert `"herdr_engineering.control_plane.artifact_share_api" not in sys.modules` within a fresh subprocess run of the tick). The subprocess imports only plan 3's modules and registrations, which is the "members disabled" configuration. Plan 5's share handlers must import that module lazily, inside the handler, so this test keeps holding.
    * A symlinked intent file is skipped and never followed.
  * **Lanes:**
    * Two intents from one agent are applied in order and never concurrently, while an intent from a second agent is applied in the same tick.
    * A test kind registered with a `lane_key` that maps two different actors to one lane runs their intents serially.
  * **Registry and actors:**
    * A test kind `x.test` (`actors={"user"}`) sent by a user actor reaches its handler with `ctx.agent is None` and `ctx.extras` as passed, and its result file carries `actor: {kind: "user", …}`.
    * The same kind's replay by `idempotency_key` uses its own `replay_outcomes`.
    * An unregistered kind is `rejected`.
    * A user actor sending `commit` is `rejected`.
    * `ctx.assemble(..., charge=False)` under owner key `m_test` verifies and stores the upload and records it uncharged. The agent's `bytes_last_24h` is unchanged.
  * **Sweeper:**
    * It removes an upload older than the TTL, an upload of a revoked agent and an upload whose write grant is gone.
    * It keeps an upload referenced by a pending intent, and a dir younger than 60 s without `meta.json`.
    * With a test prefix `m_` registered and `keep` returning True, an `m_x` dir with no grant survives. It is removed past the TTL or once `keep` returns False.
  * **No secrets in logs:** the committed text `SECRET-SENTINEL-…` and the staged bytes never appear in `caplog`. The result file of a `rejected` intent does not contain the text.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_merge_commit.py` and the RustFS tests, and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): apply collaborator commit intents and staged uploads in the worker`.

### Task 6: Collaborator lineage route and poster

**Files:** Modify `herdr_engineering/control_plane/api.py` (add `_artifact_collab_lineage` beside `_artifact_job_lineage`), `herdr_engineering/control_plane/artifact_lineage.py` (add `collab_lineage_poster_from_section`); Test `tests/control_plane/test_artifact_collab_lineage.py`.

**Interfaces:**
* `POST /api/v1/artifacts/<id>/versions/<vid>/collab-lineage` with body `{}` (any key gives 400). It needs `edit` on the artifact.
  * It reads the version from the registry, and answers 409 unless `created_via == "collaborator"`.
  * It answers 409 when the web's in-memory `artifact_graph_gate.version_retracted` holds. This is the web-side gate check.
  * It emits with `emit_version_lineage(…, scope=visibility_fields(gated=True))`, which already emits one supersession edge per parent.
* `collab_lineage_poster_from_section(section) -> Callable[[dict], dict] | None` reuses `control_plane.artifact_worker.lineage` (`api_url`, `key_file`, `timeout_seconds`) and the validation of `lineage_poster_from_section`.

- [ ] **Step 1: Write failing tests:**
  * An owner version gives 409; a collaborator merge version emits two supersession edges.
  * A retracted version (gate) gives 409.
  * An unknown version gives 404, and so does a caller without `edit`.
  * The poster builds the exact URL and headers, and its body is `{}`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `test_artifact_lineage.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): lineage for collaborator versions`.

### Task 7: collab-public artifact bridge (reads, staging, intents, feed, audit)

**Files:** Create `herdr_engineering/control_plane/collab_artifacts.py`; Modify `herdr_engineering/control_plane/collab.py`:
* add the event types `artifact_tool_used`, `artifact_commit_submitted`, `artifact_upload_staged` and `artifact_tool_denied` to `BULK_EVENTS` and wherever `workspace_terminal_authorized` is handled in `_log_limit`/`_apply`;
* add an `artifact_bridge` attribute, None by default.

Test `tests/control_plane/test_collab_artifacts.py`.

**Interfaces:**
* `class CollabArtifactBridge(service: CollabService, mirror: Path, outbox: Path, *, clock=time.time)`. Each method takes an authenticated agent `Principal` and raises `CollabError`:
  * `limits() -> BridgeLimits` comes from the fresh `grants.json` (`BridgeLimits.from_mapping`). A missing or invalid value makes every tool answer 503 "collaboration bridge unavailable".
  * `has_grants(pr) -> bool` reads `grants.json`, which must be fresh, and checks the grant is unexpired. `has_write_grant(pr) -> bool` is the same check for a `write` grant on at least one artifact (Task 8 uses it).
  * `list(pr) -> dict` returns `{artifacts: [{artifact_id, title, permission, writable, head_version_id, head_version_number}]}`.
  * `head(pr, artifact_id) -> dict`.
  * `read(pr, artifact_id, name, *, version_id=None, offset=0, max_bytes=262144) -> {name, version_id, size_bytes, offset, text, next_offset}`.
    * `max_bytes` must be ≤ 1 MiB, and slices end on a UTF-8 boundary.
    * It verifies the blob's sha256 against the manifest.
    * An unmirrored version or file gives 404 "version not mirrored", and a binary file gives 415 "binary files are not readable through collaboration".
  * `history(pr, artifact_id, *, limit=50) -> dict`.
  * `stage_chunk(pr, args) -> {upload_id, index, stored: bool, complete?: bool, received?: int, missing?: [int]}`. This is the agent tool path.
    * It needs a `write` grant on `args.artifact_id` and `writable`.
    * Rate limit: `_throttle("artchunk:"+pr.id, limits.chunks_per_minute, 60.0, "chunk")`.
    * It runs `validate_chunk`, then `stage_chunk_for(agent_owner_key(pr.id), chunk, limits=limits, staging_cap=limits.staging_bytes_per_agent)`.
  * `stage_chunk_for(owner_key: str, chunk: Chunk, *, limits: BridgeLimits, staging_cap: int) -> dict` is the owner-keyed staging core. It takes no `Principal` and checks no grant; authorization is the caller's job. Plan 5's `MemberService` calls it with a member owner key for agents and web users alike.
    * It derives `upload_id_for(owner_key, …)`.
    * On the first chunk of an upload it reserves the declared `size_bytes`. The reservation is checked against `staging_cap` (the sum of declared sizes in that owner's staging dir) and `staging_bytes_total` (all owners), and a refusal is 429 "staging space full; commit or wait". The `meta.json` itself is written with `link_new_bytes`.
    * A later chunk's declared fields must match `meta.json`, else 409.
    * The part is written with `link_new_bytes`. If the part exists with the same sha, the call is a replay (`stored: false`). If it exists with a different sha, the answer is 409 "chunk already staged with different content".
    * The progress fields `complete`, `received` and `missing` are returned only when `index == total - 1` or on a replay. `missing` holds the first ≤ 100 absent indices.
    * The progress fields come from one directory scan. A plain chunk never scans the directory, so a 190 000-part upload stays linear.
    * When the upload becomes complete, it audits `artifact_upload_staged` once (upload id, artifact, size).
  * `append_intent(actor: Actor, kind: str, payload: dict, *, artifact_id: str | None, idempotency_key: str | None) -> str` is the generic intent write. It returns the ticket.
    * It enforces the registered kind and actor via `validate_intent` on the envelope it builds, and the `max_pending_intents_per_agent` cap per actor.
    * For an agent actor, it refreshes `agents.json` first when the feed is older than 15 s.
    * It records ticket→actor in a bounded in-memory map and writes the intent with `atomic_write_json`.
    * Its callers authorize first. The plan-3 caller is `commit()`; plan 5's `MemberService` is another.
  * `read_result(actor: Actor, ticket: str, *, wait: float, still_valid: Callable[[], bool]) -> dict` is the generic long-poll.
    * It checks ownership by the ticket map AND by the `actor` in the result file. A mismatch or an unknown ticket gives the same 404 "not found".
    * It polls the file at ≤ 0.5 s intervals up to `wait` (≤ 20 s), under the existing `MAX_WAITERS` cap per actor, and stops early when `still_valid()` turns false.
    * It returns `{status: "pending"}` or the result.
  * `commit(pr, args) -> {ticket, status: "pending"}`. It needs a `write` grant and `writable`.
    * Limits, in order:
      * `_throttle("artcommit:"+pr.id, limits.commits_per_minute, 60.0, "commit")`;
      * the `max_pending_intents_per_agent` cap;
      * `validate_changes`;
      * for each `upload_id`, its `meta.json` must exist under the **caller's** staging dir for this artifact and name, and `upload_complete` must hold (all parts, or the worker's `verified` marker). Otherwise the answer is 404 "upload not found", or 409 "upload incomplete" with `missing`. An intent whose `idempotency_key` replays a finished commit may therefore still be refused here after the TTL; the worker-side short-circuit covers retries within the TTL;
      * then `append_intent(Actor("agent", pr.id), "commit", payload, …)`.
    * It audits `artifact_commit_submitted` with ticket, artifact and byte count, never text.
  * `result(pr, ticket, *, wait: float) -> dict` is `read_result(Actor("agent", pr.id), ticket, wait=wait, still_valid=lambda: service.still_valid(pr))`.
  * `write_agents_feed() -> None` atomically writes `agents.json = {generated_at, agents: {agent_id: {owner, status, expires_at, external: bool, join_id, hard_expiry}}}`, with `join_id` from the reverse of `_joins_by_id`.
    * `_housekeeping` is request-driven, so it cannot be the trigger. A daemon thread started with the bridge writes the feed every 15 s.
    * It also runs on any agent revoke or disable, and inside `append_intent` for an agent actor when the feed is older than 15 s, before the intent is written.
* Denials:
  * A missing grant, an ungranted artifact and an unknown artifact all give the same 404 "not found".
  * Denials go through `service._audit_denied(pr, "artifact.<tool>", reason, resource_id=artifact_id)`.
  * Allowed reads are audited `artifact_tool_used` at most once per 600 s per (agent, artifact, tool).

- [ ] **Step 1: Write failing tests** with a real `CollabService` and two agents (A granted write on X, B granted read on Y), a hand-written mirror (including `limits`) and temp outbox:
  * **Listing and reads:**
    * A lists only X and B only Y.
    * A reading Y gets 404, the same as an unknown artifact, and the denial is audited.
    * `grants.json` older than 120 s, or without `limits`, makes `has_grants` False and every tool 503 "collaboration bridge unavailable".
    * A `read` slice never splits a multibyte character.
    * A blob whose sha256 differs from the manifest gives 502 "mirror integrity" and returns no text.
  * **Staging:**
    * A stages chunks 0 and 2 of 3. The reply to 2 says `complete: false, missing: [1]`.
    * A then stages 1, and a replay of 2 says `complete: true`.
    * A plain chunk reply has no progress fields.
    * The parts are 0600 under `staging/<A>/<upload_id>/`.
    * Re-sending chunk 1 gives `stored: false`; chunk 1 with different bytes gives 409.
    * Bad base64, a wrong chunk sha and a 524 289-byte chunk each give 400, and nothing is staged.
    * B staging on Y gives 403 "read-only grant".
    * A first chunk whose declared size exceeds `staging_bytes_per_agent` (set small) gives 429.
    * The `chunks_per_minute + 1`th chunk within 60 s gives 429.
    * A chunk naming `artifact_id` Y (no grant) gives 404.
    * `stage_chunk_for("m_test", chunk, …)`, called directly with no principal and no grant, stages under `staging/m_test/` and respects `staging_cap` and `staging_bytes_total`. A replay through it is idempotent too.
  * **Commits:**
    * B's `commit` on Y gives 403 "read-only grant".
    * The `commits_per_minute + 1`th commit within 60 s gives 429.
    * A 5th pending intent gives 429.
    * A commit with A's incomplete upload gives 409 with `missing`.
    * B committing (on an artifact B can write) with A's `upload_id` gives 404 "upload not found".
    * The intent file holds no `Authorization`, credential digest or device id, and no staged bytes.
    * With `agents.json` 200 s old and no request traffic, `commit()` refreshes the feed first, so the worker's freshness check passes.
  * **Generic intents:**
    * `append_intent(Actor("user","u1"), "x.test", …)` for a test-registered kind writes an intent the worker's `validate_intent` accepts.
    * `read_result` for that ticket by `Actor("user","u2")` gives 404, and by `u1` returns the result.
    * `append_intent` with an unregistered kind raises 400 before writing anything.
  * **Results:**
    * B calling `result` with A's ticket gets 404, both immediately and after waiting.
    * `result` returns the worker's file content once it appears mid-wait.
  * **Audit and feed:**
    * 100 `read` calls produce one `artifact_tool_used` event.
    * A completed upload produces exactly one `artifact_upload_staged` event, with no content.
    * `agents.json` lists the external agent's `join_id` and `hard_expiry`, and is rewritten after `revoke_agent`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `tests/control_plane/test_collab_external.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): artifact bridge for collab-public`.

### Task 8: MCP tools, the chunk body cap, and the end-to-end test

**Files:** Modify:
* `herdr_engineering/control_plane/collab_mcp.py`: `_ARTIFACT_TOOLS`, `ARTIFACT_TOOL_NAMES`, `tools_for`, `_call`;
* `herdr_engineering/control_plane/api.py`: `_jsonrpc`;
* `deploy/control-plane/collab-edge/nginx.conf`: the `/mcp` location.

Tests: `tests/control_plane/test_collab_mcp_artifacts.py`, `tests/e2e/test_artifact_collab_bridge.py`.

**Interfaces:**
* Tool schemas follow spec B's table, plus the seventh tool. All of them have `additionalProperties: False`.
  * `artifacts_list {}`, `artifact_head {artifact_id}`, `artifact_read {artifact_id, name, version_id?, offset?, max_bytes? (1..1048576)}`, `artifact_history {artifact_id, limit? (1..200)}`.
  * `artifact_stage_chunk {artifact_id, name, size_bytes (≥1), file_sha256, index (≥0), total (≥1), sha256, data_b64}`.
  * `artifact_commit {artifact_id, base_version_id, message (≤1000), changes (array of {name, op: put|delete, text?, upload_id?}), idempotency_key?}`. The changes array has no schema `maxItems`, because `max_changes_per_commit` is configurable and enforced in code.
  * `artifact_commit_result {ticket, seconds? (0..20)}`.
* `tools_for(service, pr)` appends `_ARTIFACT_TOOLS` only when `service.artifact_bridge` is set and `artifact_bridge.has_grants(pr)` holds. `_call` raises 404 "unknown tool" for those names otherwise.
* Wrapping:
  * Contents, history and results are wrapped `untrusted=True`.
  * The `head` and `list` manifests are wrapped `untrusted=True` because they carry names chosen by others.
  * The commit ticket reply and the stage-chunk reply are not wrapped.
* Request bodies in `_jsonrpc`, for label `"MCP"` only:
  * A `Content-Length` ≤ `MAX_BODY` behaves as today.
  * Admission registry: `register_large_body_admission(predicate: Callable[[CollabService, Principal], bool], tools: frozenset[str])` in `collab_mcp.py`. Plan 3 registers `(lambda s, pr: s.artifact_bridge is not None and s.artifact_bridge.has_write_grant(pr), {"artifact_stage_chunk"})`. Plan 5 registers `(member_check, {"artifact_upload_chunk"})`, which admits member agents with no write grant.
  * For `MAX_BODY` < length ≤ `MCP_UPLOAD_MAX_BODY` (Task 3), it **authenticates first** (`service.authenticate` from headers). It computes the union of `tools` over every admission whose predicate holds.
    * If the union is empty, it answers 413 without reading **and with `Connection: close`**, so unread bytes are never parsed as a next request (the share routes' precedent).
    * Otherwise it reads the body.
  * After parsing, a body over `MAX_BODY` whose `params.name` is not in that union is answered with JSON-RPC error `-32602 "request too large for this tool"`.
  * Anything over `MCP_UPLOAD_MAX_BODY` gives 413 for everyone.
  * nginx sets `client_max_body_size 768k` in `location = /mcp` only. The server-level 64k stays for every other route.
  * Plan 5 extends this same gate through the registry. It must not add a second one.

- [ ] **Step 1: Write failing tests:**
  * **Tool listing:**
    * An agent without grants gets a `tools/list` with none of the seven tools, and `tools/call artifacts_list` gives the error "unknown tool".
    * A granted agent gets all seven.
    * No tool name in any agent's list contains `share`, `acl` or `grant`, for internal and external agents with every grant combination.
  * **Arguments and wrapping:**
    * An unknown argument gives 400 for every tool.
    * `artifact_read` output starts with the `UNTRUSTED` notice.
  * **Body limits:**
    * A 700 KiB body from an unauthenticated caller gives 413 with `Connection: close`, and the body is never read: assert with an `rfile` that raises if read past 0 bytes.
    * The same body from an agent with only read grants gives 413.
    * A write-granted agent's 700 KiB `artifact_stage_chunk` (one full 512 KiB chunk, base64) is accepted.
    * A write-granted agent's 700 KiB `artifact_commit` gives `-32602`.
    * A test-registered admission (always true, tools `{"x_chunk"}`) admits a 700 KiB `x_chunk` from an agent with no grants, and gives `-32602` for its 700 KiB `artifact_stage_chunk`. The registry is reset afterwards.
    * 800 KiB gives 413 for everyone.
    * `tests/test_compose_render.py`, or the existing nginx config test, asserts the 768k limit appears only in `/mcp`.
  * **End to end:** `test_artifact_collab_bridge.py` uses Postgres, a fake RustFS, a temp mirror/outbox, an in-process `CollabService` and `CollabArtifactBridge`. It drives `collab_mcp.handle` plus `CollabMirror.sync`, `CollabIntentApplier.tick` and `sweep_staging`, and covers:
    * A grants write on X and the owner commits v2.
    * A reads v1 via MCP and commits a disjoint edit with base v1. A `merged` result arrives through `artifact_commit_result`, and `artifact_history` shows the merge with two parents.
    * A then commits an overlapping edit and gets `conflict` with markers, resolves it, and commits with `base = head_version_id` to get `created`.
    * A stages a 1.3 MiB binary file in 3 chunks, interrupted after chunk 2 and resumed from `missing`. A commits it with `upload_id` and gets `created`, and the manifest shows its sha256 and size.
    * The owner revokes. Within 2 ticks `artifacts_list` is empty, and a commit is refused 404 in collab-public, or `rejected` if it was already queued. A's leftover staging for X is swept.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** both tests plus the existing MCP tests (`tests/control_plane/test_collab_external.py`, `test_collab_pentest.py`, `test_collab_bounded.py`) and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): artifact collaboration MCP tools with chunked staging`.

### Task 9: Wiring, deployment and the fast worker loop

**Files:** Modify:
* `herdr_engineering/control_plane/worker.py`: add `CollabBridgeLoop`;
* `herdr_engineering/cli.py`: `_collab_artifacts_settings(cp)` (which builds `BridgeLimits.from_mapping`), `_cmd_cp_worker` (start the loop), and the collab-only server flags `--collab-artifacts-mirror` / `--collab-artifacts-outbox` that construct `CollabArtifactBridge`;
* `deploy/control-plane/compose.yaml`: worker mounts;
* `deploy/control-plane/compose.collab.yaml`: `collab-public` mounts and flags;
* `deploy/control-plane/deploy.sh`: export and create `HERDR_COLLAB_MIRROR_DIR`, `HERDR_COLLAB_OUTBOX_DIR` and `HERDR_COLLAB_SPOOL_DIR`, as for the share dirs;
* `deploy/control-plane/fleet-config.example.yaml`: `control_plane.collab_artifacts` with every key of the Configuration table, commented with its default.

Tests: `tests/control_plane/test_worker.py` (extend), `tests/test_compose_render.py` / `tests/test_share_deploy_config.py` (extend).

**Interfaces:**
* `CollabBridgeLoop(dsn_connect: Callable[[], Awaitable[Connection]], mirror: CollabMirror, applier: CollabIntentApplier, *, interval=1.0, mirror_every=30.0, retractions_every=30.0)` with `async run(stop: asyncio.Event)`. It runs two tasks, and neither uses the worker pass's connection:
  * **Publisher task**, on its own connection. Each tick runs `publish_grants`, `resolve_invite_grants` from `agents.json` (Task 1), the throttled `mirror.sync`, the `load_retractions` refresh, `sweep_staging` and `sweep_results`.
  * **Applier task**, on a small pool of `limits.apply_concurrency` connections. It runs `applier.tick` every `interval`.
  * Each task logs its own exceptions and reconnects after 5 s. A failing applier never stops the publisher, and the reverse holds too.
* `_cmd_cp_worker` runs it as a separate `asyncio` task beside `run_worker` when `collab_artifacts.enabled is True`. It builds the applier with `context_extras={}` and registers only `commit`; plan 5 adds its kinds, extras and staging owner here. An invalid limits section fails the worker start with a clear message.
* Mounts:
  * The worker mounts `/collab-mirror` rw, `/collab-outbox` rw and `/collab-spool` rw.
  * `collab-public` mounts `/collab-mirror:ro` and `/collab-outbox` rw.
  * Neither sees the other's private volumes. `collab-public` still has no Postgres or RustFS.

- [ ] **Step 1: Write failing tests:**
  * **Loop:**
    * The publisher and applier connections differ from each other and from `run_worker`'s (injected fakes).
    * A failing tick in either task does not stop the loop.
    * With a fake `put_stream` that blocks for 5 s, `grants.json` keeps being rewritten on its heartbeat, and a second agent's intent is applied.
    * With `enabled` absent or `false`, no loop starts and `collab-public` starts no bridge.
  * **Config:**
    * `daily_bytes_per_agent` from YAML reaches the applier and `grants.json` `limits`.
    * `daily_bytes_per_agent: 0` fails the start.
    * No `os.environ` read is introduced: grep-style test over the new modules.
  * **Compose and deploy:**
    * The compose render keeps the mount table in step: `collab-mirror` is `:ro` in `collab-public` and rw in the worker, the `outbox` mount is present in both, and `collab-spool` is in the worker only. `collab-public` still has no `env_file`, no Postgres network and no new capabilities.
    * `deploy.sh` creates the mirror and outbox dirs owned by `HERDR_COLLAB_CERT_UID` with mode 0755, as it does for the share dirs, and the spool dir root-owned 0700.
    * The worker (which has no `user:`, so it runs as root) writes mirror files 0644 and dirs 0755, and can read 0600 staged parts and unlink in the outbox. A deploy test pins these modes.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the extended tests plus `tests/test_deploy_scripts.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(deploy): collaborator bridge directories and the 1 s worker loop`.

### Task 10: Owner routes for collaborators, activity, version diffs and the retraction fail-safe

**Files:** Create `herdr_engineering/control_plane/artifact_collab_api.py` (hook `route_collab` into `_route_api` beside `route_share`); Modify:
* `herdr_engineering/control_plane/artifacts.py`: `list_version_summaries` adds `provenance->>'message' AS message`; add `version_text_diff`;
* `herdr_engineering/control_plane/artifact_retraction.py`: `retract(..., on_unpersisted=None)`;
* `herdr_engineering/control_plane/api.py`: `_artifact_retract` passes the fail-safe and honours the operator's `restrict_all` request.

Tests: `tests/control_plane/test_artifact_collab_api.py`, `tests/control_plane/test_artifact_retraction.py` (extend).

**Interfaces:** All routes need `admin` on the artifact (via `_access`), except `diff`, which needs `read`. Denials are a non-disclosing 404. A POST with a foreign `Origin` gets 403. When `collab_artifacts.enabled` is not true, every route answers 503 "collaboration is not enabled".
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
  * The `diff` is `difflib.unified_diff` text, capped at 2 MiB per file and 4 MiB in total, then `reason: oversize`. These caps are for display only: they bound `difflib` cost and are not storage limits.
  * Implemented as `ArtifactStore.version_text_diff(artifact_id, version_id) -> dict`.
* Retraction fail-safe (Decision 7):
  * `retract(..., on_unpersisted: Callable[[str], int] | None = None)` calls `on_unpersisted(artifact_id)` only when `persist` raised.
  * The result gains `collab_write_grants_revoked: int | None` and `collab_revoke_error: str | None`. A failing callback is caught and reported, and the route still answers 500 with both errors.
  * `_artifact_retract` passes a callback that runs `CollabGrantStore(conn).revoke_write_grants(artifact_id)` through the registry's call path. The worker's next `publish_grants` then drops those grants from `grants.json`.
  * The callback runs for version retractions as well as artifact retractions.
  * Optional restrict-all (operator action only, never automatic):
    * When `persist` raised, the 500 body also carries `restrict_all_offered: true`, so clients can ask "restrict all access now?".
    * `POST /api/v1/artifacts/{id}/collab-restrict` (needs `admin`) runs `CollabGrantStore.revoke_all_grants(artifact_id)` and returns `{revoked: int}`. The worker's next `publish_grants` and mirror sync then drop the grants and remove the artifact from the mirror.
    * `_artifact_retract` accepts `restrict_all: true` in the body and then runs the same revocation after the write-grant fail-safe. Without that flag, read grants are never touched.

- [ ] **Step 1: Write failing tests:**
  * **Access:**
    * A non-admin caller gets 404 on every route; a cross-origin POST gets 403.
  * **Grants:**
    * A grant writes the `collab:` row and the web's in-memory `server.artifact_acl` is unchanged.
    * An internal agent can be granted `write`.
    * `write` on a report gives 409.
    * `expires_in_days: 91` gives 400, an unknown body key gives 400, and an omitted `expires_in_days` gives 30 days.
    * Revoke removes the row.
  * **Activity, diffs and summaries:**
    * Activity lists a conflict with its names.
    * The diff of a merge version is against its first parent, and a binary file gets `reason: binary`.
    * A hostile file name round-trips as data (JSON), never interpreted.
    * Version summaries include `message`.
  * **Retraction fail-safe:**
    * When `persist` raises, the callback runs. The artifact's write grants are gone and its read grants remain, and the result carries `collab_write_grants_revoked: 1`.
    * When `persist` succeeds, the callback never runs.
    * When the callback raises, the result carries both errors and is a 500.
  * **Restrict all (optional):**
    * A failed-persist retraction without `restrict_all` leaves read grants and answers `restrict_all_offered: true`.
    * With `restrict_all: true`, a failed-persist retraction also removes the read grants.
    * `POST …/collab-restrict` removes read and write grants of that artifact only, needs `admin` (a non-admin gets 404, a cross-origin POST gets 403), and the mirror drops the artifact within one sync.
    * No code path calls `revoke_all_grants` automatically: a successful retraction, and a failed one without the flag, never do.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_share_api.py` and the retraction tests, and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): owner routes for collaborators and the retraction fail-safe`.

### Task 11: Owner CLI

**Files:** Create `herdr_engineering/artifact_collab_client.py` with `add_collab_subparser(asub)`, registered in `cli.py` after `add_share_subparser`; Test `tests/test_artifact_collab_client.py` (follow the share client tests).

**Interfaces:** These call Task 10's routes with the CLI's existing API-key client and print JSON:
* `herdr-eng artifacts collab list <artifact_id>`;
* `… grant <artifact_id> <agent_id> [--write] [--days N]` (1–90, default 30);
* `… revoke <artifact_id> <agent_id>`;
* `… restrict <artifact_id>` calls `POST …/collab-restrict`;
* `herdr-eng artifacts retract <artifact_id> [--restrict-all]` (the existing command gains the flag, which sends `restrict_all: true`).

- [ ] **Step 1: Write failing tests:** each command hits the exact path, method and body (`retract` sends `restrict_all` only with `--restrict-all`), and `--days 0` and `--days 91` are refused locally. **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS. **Step 5: Commit** `feat(cli): artifacts collab grant/list/revoke/restrict`.

### Task 12: Share panel Collaborators and Activity tabs

**Files:** Modify:
* `packages/herdr-web-client/client.js`: add the methods;
* `packages/artifact-share-panel.js`: tabs Links | Collaborators | Activity, where the Links tab is today's content unchanged.

Create `packages/artifact-collaborators.js` and `packages/artifact-activity.js`. Tests: `packages/herdr-web-client/artifact-collab.test.js`, `packages/artifact-collaborators.test.js`, `packages/artifact-activity.test.js`.

**Interfaces:**
* Client methods (all `{signal} = {}`):
  * `listArtifactCollaborators(id)`, `grantArtifactCollaborator(id, {agent_id, permission, expires_in_days})`, `revokeArtifactCollaborator(id, agentId)`, `grantArtifactInvite(id, {invite_id, permission, expires_in_days})`, `artifactCollabActivity(id)`, `artifactVersionDiff(id, versionId)`, `restrictArtifactCollab(id)`;
  * `listCollabAgents()` → `GET /api/v1/collab/agents`;
  * `createExternalInvite(roomId, body)` → the existing externals invites route.
* Renderers, pure, returning escaped HTML:
  * `renderCollaboratorRow(c, {busy})` shows agent, permission, expiry, last used, commits, conflicts and Revoke.
  * `renderGrantForm(state)` has an agent `<select>`, a Read/Read+write choice disabled with `writable_reason` when not writable, and days 1–90 (default 30).
  * `renderInviteResult(invite)` shows the code once, with the same once-only and clear-on-close rules as the share URL.
  * External invites are room-bound, so the invite form has a room `<select>` filled from the operator's rooms. Reuse the existing collab rooms client call that `collab-app.js` / `collab-view.js` use (`GET /api/v1/collab/rooms` through the dashboard proxy), adding a `listCollabRooms()` client method if none exists.
  * `renderRestrictAll(state)` shows a **Restrict all access** button with a confirm step. It is offered in the Collaborators tab, and is highlighted when a retraction answered `restrict_all_offered`. It is never triggered automatically.
  * `renderHistoryRow(v)` shows number, author label, message, parents and a merge badge.
  * `renderAttempt(a)` shows outcome, agent, conflict names and kinds, reason and time.
* Mounting:
  * `mountCollaborators(container, client, artifactId, {signal})` and `mountActivity(container, client, artifactId, {versions, signal})`.
  * Activity's "Show rejected attempts" toggle loads lazily.
  * Selecting a version renders the file list and `renderDiff` (from `herdr-web-components/inspector.js`) for each modified file. A file with `reason: binary|oversize` shows the reason instead of a diff.

- [ ] **Step 1: Write failing tests:**
  * The client hits exact paths and bodies, with ids `encodeURIComponent`-ed.
  * A hostile agent name, message, file name or conflict name (`<img onerror>`) is escaped.
  * A non-writable artifact disables Read+write and shows the reason.
  * The Restrict all access button needs a confirm, then calls `restrictArtifactCollab` once; nothing calls it without a click.
  * A 503 from `listArtifactCollaborators` shows "Collaboration is not enabled" with the form hidden, and the Links tab still works.
  * A failed `listCollabAgents` shows an agent-id text input instead of the picker.
  * The invite code disappears from the DOM on Close.
  * The rejected toggle fetches only once.
  * A merge version shows two parents and the badge.
  * A binary file in a diff shows "binary" with no diff body.
  * Buttons are disabled while their call is in flight.
- [ ] **Step 2: Run** `node --test` on the three files and expect FAIL. **Step 3: Implement.** **Step 4: Run** `npm run test:client` and expect PASS.
- [ ] **Step 5: Commit** `feat(web): collaborators and activity tabs`.

### Task 13: Playwright end-to-end for the tabs

**Files:** Extend `tests/playwright/artifact-shares-server.py` (in-memory collaborators, activity, diff and collab agents routes) and `tests/playwright/artifact-shares.test.mjs`.

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
* `packages/collab-guide.md` and its mirror `docs/operations/collaboration-network-guide.md`. Keep them identical, and run any test that checks this.
* `skills/herdr-artifact/SKILL.md`: a short "collaborating through MCP" reference.
* `deploy/control-plane/COLLAB.md`: the bridge, staging and spool dirs on large storage, the enable switch, the full `control_plane.collab_artifacts` key table, and rollback = `enabled: false`.

- [ ] **Step 1:** Document:
  * the seven tools with arguments and results;
  * the staging loop:
    * chunk the file at 512 KiB and send each chunk with its sha256;
    * resume from `missing`;
    * commit with `upload_id`;
    * know that a re-sent chunk is harmless;
  * when inline `text` is enough (small text under the 64 KiB request) and when to stage (anything larger, and binary files);
  * the commit/merge/conflict loop: resolve, then re-commit with `base = head_version_id`. Binary and over-`max_text_merge_bytes` files changed on both sides conflict as `binary`/`oversize` with no merged text;
  * the limits, as configurable defaults: 100 changes per commit, 30 commits/min, 1200 chunks/min, 100 GB/day, 60 s intent age, a 24 h staging TTL, plain-artifact-only writes;
  * the honest promise that revocation takes effect within seconds;
  * that collaborators can never share.
- [ ] **Step 2: Run** the docs or guide tests (`grep -rl collab-guide tests/`) and expect PASS. **Commit** `docs(collab): artifact collaboration tools`.

---

## Contract for plan 5 (member-owned artifacts)

Plan 5 (`2026-10-30-member-owned-artifacts.md`, "Needed from plan 3") depends on the following. Plan 3 lands these extension points with tests and registers nothing for members.

| # | Plan 5 needs | Provided by plan 3 |
|---|---|---|
| 1 | Intent kinds through a handler registry | `register_intent_kind` (Task 3) and `CollabIntentApplier.register(kind, handler, *, lane_key, replay_outcomes)` with `IntentContext` (Task 5) |
| 2 | A web user as intent sender | `Actor(kind="user")` in the envelope, the ledger and results (Tasks 1, 3, 5 and 7). `commit` stays agent-only. |
| 3 | One member's intents serial | `lane_key` per kind (Task 5). Plan 5 maps every actor of a member to `member:<user_id>`. |
| 4 | Sweep keeps member staging | `register_staging_owner("m_", keep)` (Task 5). Plan 5 supplies `keep` (member valid). |
| 5 | Intent write and long-poll for any kind | `CollabArtifactBridge.append_intent` / `read_result` (Task 7) |
| 6 | `/mcp` 768 KiB gate admits member agents | `register_large_body_admission(member_check, {"artifact_upload_chunk"})` (Task 8) |
| 7 | Staging keyed by owner, with no grant | `stage_chunk_for(owner_key, chunk, *, limits, staging_cap)` (Task 7), and `agent_owner_key` refuses `m_` (Task 3) |
| 8 | Member assembly not charged to the 100 GB/day agent quota | `ctx.assemble(..., charge=False)` and `artifact_collab_uploads.charged` (Tasks 1 and 5) |
| 9 | Share module never imported by plan 3's tick | Task 5's subprocess test runs with only plan 3's registrations. Plan 5's `member.share_*` handlers import the share module lazily, inside the handler. |

Plan 5 supplies `share_store` and `server` (for `approve_share`) through `CollabIntentApplier(context_extras=…)`, which Task 9 wires as an empty mapping until plan 5 fills it.

**Not provided by plan 3, so plan 5 owns it:** `write_secret_result(ticket, payload)` (0600, read-once). Plan 3's results live in the mirror, which is read-only in `collab-public`. The worker runs as root, so a 0600 file it writes is unreadable by `collab-public`'s uid, and `collab-public` cannot delete it to make it read-once. Plan 5 must design that path itself, for example a per-ticket file collab-public can read plus a deletion request through the outbox.

**Plan 4** registers `annotation_resolve` through the same registry, instead of widening `Intent.kind` to a `Literal`.

## Open questions for the operator

None. The 2026-10-30 questions are resolved under Decisions.

## Self-review notes

* **Spec coverage:**
  * B's tools are covered by Tasks 7–8; the seventh tool, `artifact_stage_chunk`, by Tasks 3, 7 and 8 (Decision 1).
  * The mirror is Task 4. The intents queue and fast loop are Tasks 3, 5 and 9. The worker as authority is Task 5, which also does staging assembly and verification.
  * `collab:` ACL rows: Tasks 1–2 and 10. Commit semantics and merge rules: plan 2's `merge_commit`, driven by Task 5. Large and binary files take the whole-file path, with `binary`/`oversize` conflicts (Decision 2).
  * Scope (writes to plain file artifacts only): Tasks 4, 5 and 10.
  * History model: plan 2, plus `message` in Task 10.
  * Limits (all configurable, Decisions 3–4): Tasks 3 (`BridgeLimits`), 4 (published), 5 and 7 (enforced) and 9 (YAML).
  * Audit and safety: Tasks 3, 5, 7 and 8. Owner revert: plan 1.
  * Docs: Task 14. CLI: Task 11. `agents.json`: Task 7, consumed in Tasks 5 and 9. The invite+grant step: Tasks 1, 7, 9, 10 and 12.
  * Retraction fail-safe and the optional, operator-driven restrict-all (Decision 7): Tasks 1 (`revoke_write_grants`, `revoke_all_grants`), 10, 11 and 12.
  * Section C is covered by Tasks 10, 12 and 13.
  * Out of scope: annotations (plan 4) and member-owned artifacts (plan 5). Hosting is unchanged (Decision 8).
* **Spec deviations (operator-approved, 2026-10-30):**
  * There are seven tools, not six.
  * The spec's 2 MiB-per-commit and text-only writes are replaced by chunked staging of any content; `max_text_merge_bytes` bounds only text merging.
  * The 50 MiB quota becomes 100 GB/day and is configurable.
* **Retraction:**
  * The durable state and the worker-local gate are checked in Task 5. The web's in-memory gate is checked at lineage emission in Task 6.
  * If the web hides an artifact but fails to persist the retraction, the route revokes the artifact's collaborator write grants (Task 10). Commits stop within one publisher tick.
  * The remaining residual is a failed revoke, which is reported and normally coincides with Postgres being unreachable, which also blocks `merge_commit`.
  * Read access in that window stays until the operator chooses "restrict all access now" (Decision 7); it is never revoked automatically.
* **Cross-plan contract:**
  * Plan 5's nine "Needed from plan 3" items are mapped in "Contract for plan 5"; eight are provided here with tests.
  * Plan 5 must own `write_secret_result`, and plan 4 should switch `annotation_resolve` to the kind registry. Neither of those plans is edited here.
  * Plan 5 imports `CHUNK_BYTES`/`MCP_UPLOAD_MAX_BODY` from `collab_bridge_files` and extends the Task 8 gate through its registry. Its nginx `/mcp` value is already the same 768k.
* **Name and type consistency:**
  * Classes: `CollabGrantStore` (incl. `revoke_write_grants`, `revoke_all_grants`, `decided_for_key(actor, kind, key, outcomes)`, `verified_upload(owner_key, …)`, `record_verified_upload(…, charged)`), `CollabMirror(root, *, limits)`, `CollabIntentApplier(outbox, mirror, *, rustfs, limits, spool_dir)` (incl. `register`, `register_staging_owner`, `sweep_staging`), `IntentContext` (incl. `assemble(…, charge)`, `extras`), `CollabArtifactBridge` (incl. `stage_chunk`, `stage_chunk_for`, `append_intent`, `read_result`, `limits`), `CollabBridgeLoop`.
  * Contract: `BridgeLimits`, `validate_intent(…, limits)`, `validate_changes(…, limits)`, `validate_chunk -> Chunk`, `upload_id_for`, `staging_path`, `StagedUpload`, `Actor`, `agent_owner_key`, `register_intent_kind`, `Intent` (`kind, actor, artifact_id, idempotency_key, submitted_at, payload`), `CommitPayload` (`inline_bytes`), `Change` (`name, op, text, upload_id`), `register_large_body_admission`.
  * Ledger columns: `artifact_collab_intents.kind`/`actor_kind`/`actor_id` replace `collaborator_agent_id`, and `artifact_collab_uploads.owner_key`/`charged` replace `agent_id`.
  * Constants and patterns: `CHUNK_BYTES`, `MCP_UPLOAD_MAX_BODY`, `TICKET_RE`, `UPLOAD_RE`, `INTENT_MAX_AGE`, `FEED_MAX_AGE`.
  * Tables: `artifact_collab_intents`, `artifact_collab_invite_grants`, `artifact_collab_uploads`.
  * Config: `control_plane.collab_artifacts`.
  * Removed: `MAX_COMMIT_TEXT_BYTES`, `MAX_INTENT_FILE_BYTES`, `MAX_MCP_COMMIT_BODY`, the 7 MiB `/mcp` body and the 64 MiB outbox-bytes cap. Intents are now ≤ 64 KiB and bounded by `max_pending_intents_per_agent`, and staging is bounded by the staging caps.
