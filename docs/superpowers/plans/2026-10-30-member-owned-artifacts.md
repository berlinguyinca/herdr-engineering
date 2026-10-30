# Member-owned artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 5 of 5 (spec section E): signed-in group members create, publish and lab-share their own artifacts through the collaboration MCP server and the artifact skill, under hard quotas, and can never create a public link.

**Architecture:** Member agents call new MCP tools in `collab-public`; every write becomes an intent on plan 3's intents queue; the control-plane worker re-checks membership, ownership and quota against Postgres and applies it as principal `member:<user_id>`. File bytes travel as hash-checked chunks staged on the intents volume and assembled by the worker through the existing `ArtifactUploadMixin`. The public-link refusal lives in `ShareStore` (create *and* mark_live) so no route — HTTP, CLI, MCP, forged intent — can bypass it. The operator gets a Members panel; members get no web UI (spec, second builder check).

**Tech Stack:** Python 3.12 / asyncpg, collab MCP (JSON-RPC over HTTP), Postgres migrations in `deploy/control-plane/migrations`, plain ES-module dashboard (`packages/`), node:test + Playwright; POSIX sh + Python client in the separate `herdr-artifacts` repo.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md` — section E, "Second builder check: resolutions", "Builder-check resolutions", "Delivery order" item 5. Share-link rules: `docs/superpowers/specs/2026-10-30-artifact-share-links-design.md`. Plans 1–2: `docs/superpowers/plans/2026-10-30-artifact-share-panel-and-merge-engine.md` (merged on this branch: `merge_commit`, migration 0043).

## Global Constraints

* **Members can never create a public link** — server-enforced in `ShareStore.create` and `ShareStore.mark_live`, refusal audited, whatever the entry point. Public links stay operator-only. Refusal error code: `member_public_refused`.
* Member principal: `member:<collab user_id>` where `user_id` is the collab owner id of the agent (the Cognito `username` claim, falling back to `sub`, as `CognitoAuthenticator.verify_claims` does). Spec says "cognito sub"; the collab identity is the username — one function (`member_principal`, Task 1) is the only derivation. Ownership = the existing `artifacts.created_by`; no `owner_principal` column.
* A member is: a collab agent that is active, **not external**, whose owner is an approved, non-disabled user in `collab.cognito_group`, and whose owner is not sharing-disabled by the operator. External agents are never members.
* Quotas (defaults, operator-overridable per member): 25 MiB (26_214_400 B) per artifact version bundle, 200 MiB (209_715_200 B) stored per member, 20 live artifacts per member (`lifecycle='active'`). Bundles larger are an operator action.
* Member lab shares: `audience` must be `lab`; `allow_groups ⊆ {collab.cognito_group}`; every `allow_emails` entry must be a known pool user's email. Expiry: same rules as the operator (lab 1–3650 days, optional). Summary → explicit approve with `confirm: true`.
* Member content types: the share rules (HTML, PDF, HTML + parquet); same bundle scanner and sandbox CSP; members' lab links show the banner "Shared by <name> — content was not reviewed by the operator".
* Chunk: raw part ≤ 512 KiB (524_288 B), sent base64 with its sha256; MCP request bodies above the existing `MAX_BODY` (64 KiB) are accepted **only** for an authenticated member agent calling `artifact_upload_chunk`, up to `MCP_UPLOAD_MAX_BODY = 768 * 1024`.
* Intents expire after 60 s (plan 3); a disabled/removed member's pending intents are rejected; revocation takes effect within seconds, not instantly.
* Approve URL travels back once: a 0600 result file readable only by worker and `collab-public`, deleted on first read, swept after 60 s; never logged, never in the event log, never in the mirror.
* Configuration is YAML only (`control_plane.collab.members`, `control_plane.share`); no new environment variables (operator rule).
* Migration: `0046_member_artifacts.sql` (0043 is on this branch; 0044/0045 are reserved for plans 3/4). If main has moved past, take the next free number at implementation time.
* Python style as surrounding code (`from __future__ import annotations`, asyncpg conn in stores, `ArtifactError`/`ShareError`, hand-validated MCP args with `additionalProperties: False`). Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Consumed from plan 3 (bridge) — contract

Plan 3 is not written yet. This plan relies on the following names, taken from spec section B / second builder check. If plan 3 ships different names, adapt the call sites, not the behaviour.

* `herdr_engineering/control_plane/artifact_bridge.py`:
  * collab side: `append_intent(root: Path, *, kind: str, agent_id: str, payload: dict, idempotency_key: str) -> str` (intent id; payload stamped with `created_at`), `read_result(root: Path, intent_id: str, *, wait: float) -> dict | None` (bounded long-poll like `tasks_wait`; `{"status": "pending"}` while waiting).
  * worker side: `IntentLoop.register(kind: str, handler: Callable[[IntentContext, dict], Awaitable[dict]])`; `IntentContext` exposes `agent_id`, `agent` (row from the agents feed), `store` (ArtifactStore), `share_store`, `server` (for `approve_share`), and `write_secret_result(intent_id, payload)` (0600, read-once). The loop already rejects intents older than 60 s and agents not `active` in the feed.
* `agents.json` feed written by `collab-public`: `{agent_id: {"owner_user_id", "status", "external": bool, "groups": [..], "harness_type"}}`.
* `collab_mcp._ARTIFACT_TOOLS` and the grant-gated branch of `tools_for` / `_call`. This plan adds a sibling list `_MEMBER_ARTIFACT_TOOLS` and does not touch grant tools (`artifacts_list`, `artifact_head`, `artifact_read`, `artifact_history`, `artifact_commit`, `artifact_commit_result`) beyond letting a member call `artifact_history`/`artifact_head` on artifacts they own (Task 6).
* Plan 4 (annotations) owns `artifact_annotations` / `artifact_annotation_resolve`; members reach them for own artifacts through the same ownership check (Task 6), no new tools here.

## Review Focus

* **Cross-member access:** member A naming member B's `artifact_id`/`share_id`/`upload_id` in any tool or intent gets the same non-disclosing "not found" as a nonexistent id — never "forbidden", never B's metadata. Test per tool (Tasks 4, 6, 7).
* **Quota race:** two concurrent publishes/creates by one member that individually fit but together exceed 200 MiB or 20 artifacts — exactly one succeeds; reserved bytes are released on abort/expiry/failed commit, so a failed upload never permanently eats quota (Task 3).
* **Audience widening without "public":** a member lab share with `allow_groups: ["admins"]`, an outside email, an uppercase/whitespace variant of the group, or an empty allow-list is refused; a forged intent or a direct `ShareStore.create` with `audience: "public"` from `member:`/`collab:` is refused and audited; a pre-existing public row on a member artifact can never be marked live (Tasks 2, 7).
* **Lost or doubled share URL:** an approve whose result file is read twice yields the URL once and `already_read` after; a retry with the same idempotency key after the URL was consumed does not mint a second token (Task 7).
* **Disabled member mid-flight:** a member removed from the group, sharing-disabled by the operator, or whose agent is revoked between intent append and apply — the intent is rejected and audited, no version/share is created; their staged chunks are deleted (Tasks 4, 9).

---

### Task 1: Member identity and migration 0046

**Files:** Create `herdr_engineering/control_plane/artifact_members.py`, `deploy/control-plane/migrations/0046_member_artifacts.sql`; Test `tests/control_plane/test_artifact_members.py` (Postgres fixtures as in `test_share_store.py`).

**Interfaces:**
- Migration (additive, idempotent): `artifact_member_usage(member_principal text PK, max_artifact_bytes bigint NOT NULL DEFAULT 26214400, max_member_bytes bigint NOT NULL DEFAULT 209715200, max_artifacts int NOT NULL DEFAULT 20, bytes_used bigint NOT NULL DEFAULT 0, bytes_reserved bigint NOT NULL DEFAULT 0, sharing_disabled boolean NOT NULL DEFAULT false, display_name text, last_used_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now(), CHECK (bytes_used >= 0 AND bytes_reserved >= 0))`; `artifact_member_events(event_id bigserial PK, member_principal text NOT NULL, agent_id text, action text NOT NULL, artifact_id text, outcome text NOT NULL CHECK (outcome IN ('ok','refused','error')), code text, detail jsonb NOT NULL DEFAULT '{}', at timestamptz NOT NULL DEFAULT now())` + index `(member_principal, at DESC)`; `artifact_upload_sessions ADD COLUMN reserved_bytes bigint NOT NULL DEFAULT 0, ADD COLUMN reserved_for text`; index `artifacts(created_by) WHERE created_by LIKE 'member:%'`.
- Produces: `MEMBER_PREFIX = "member:"`; `def member_principal(owner_user_id: str) -> str` (validates with collab's `_USER_RE`, raises `ValueError`); `def is_member_principal(p: str | None) -> bool`; `def is_restricted_principal(p: str | None) -> bool` (true for `member:` and `collab:`); `@dataclass(frozen=True) class MemberCheck: ok: bool; principal: str | None; reason: str | None`; `def member_for_agent(agent: dict | None, *, group: str, directory: dict) -> MemberCheck` (reasons: `unknown_agent`, `agent_inactive`, `external_agent`, `not_in_group`, `user_disabled`); `class MemberStore(conn)` with `async def usage(principal) -> dict` (creates the default row on first use), `async def set_limits(principal, *, max_artifact_bytes=None, max_member_bytes=None, max_artifacts=None, sharing_disabled=None, actor: str) -> dict`, `async def record(principal, action, *, agent_id=None, artifact_id=None, outcome, code=None, detail=None) -> None`, `async def list_members() -> list[dict]` (usage + artifact count + live share count).

- [ ] **Step 1: Write failing tests**: `member_principal("alice")=="member:alice"`; `"bad user"` raises; `member_for_agent` returns each reason above (external agent with group owner → `external_agent`; agent in feed with status `revoked` → `agent_inactive`); usage row defaults equal the Global Constraints values; `set_limits` refuses negative/zero limits and records an `artifact_member_events` row with the actor; migration applied twice is a no-op; `CHECK` rejects negative `bytes_used`.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_members.py -q` → FAIL.
- [ ] **Step 3: Implement** migration and module.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(artifacts): member identity, usage and audit tables`.

### Task 2: Public-link guard in the share store and approve path

**Files:** Modify `herdr_engineering/control_plane/artifact_shares.py` (`create`, `mark_live`), `herdr_engineering/control_plane/artifact_share_api.py` (`_create` error mapping, `approve_share`); Test `tests/control_plane/test_share_member_guard.py`, extend `test_artifact_share_api.py`.

**Interfaces:**
- Consumes: `is_restricted_principal`, `is_member_principal` (Task 1).
- Produces: `ShareStore.create(..., created_by, member_groups: frozenset[str] | None = None, member_emails: frozenset[str] | None = None)`. Inside the create transaction it reads `artifacts.created_by`; if the creator **or** the artifact owner is restricted and `audience != "lab"` → `ShareError("members can only share to the lab group; public links are operator-only", code="member_public_refused")` and a `share_refused` event row (share id null → logged via `artifact_member_events`, outcome `refused`). For a restricted creator it also requires `member_groups`/`member_emails` (None → refuse, fail closed) and checks `allow_groups ⊆ member_groups` (exact, after the existing `_clean_list` normalisation) and every email ∈ `member_emails` (code `member_audience_refused`). `mark_live` re-reads the artifact owner and the share's `audience` + `created_by` and refuses with the same code if the row is public and either is restricted. HTTP `_create`/approve map `member_public_refused`/`member_audience_refused` to 403 with the message.

- [ ] **Step 1: Write failing tests**: operator principal on an operator artifact → public create OK (unchanged); `created_by="member:alice"` + `audience="public"` → `member_public_refused`, no row, one refused event; `created_by="collab:agent_x"` + public → refused; **operator** principal + public on an artifact whose `created_by` is `member:alice` → refused (default per open question Q1); a public row inserted directly by SQL for a member artifact → `mark_live` refuses and the row stays pending; member lab share with `allow_groups=["other"]` → `member_audience_refused`; with an email not in `member_emails` → refused; `member_groups=None` for a member creator → refused; HTTP `POST …/shares` audience public on a member artifact by an API-key principal that holds an explicit `share` ACL grant on it (so it passes `_access`) → 403 JSON with the `member_public_refused` message (existing share API tests still pass).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_share_member_guard.py tests/control_plane/test_artifact_share_api.py tests/control_plane/test_share_store.py -q` → new FAIL, existing PASS.
- [ ] **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(shares): members can never create or approve a public link`.

### Task 3: Quota reservation on uploads and commits

**Files:** Create `herdr_engineering/control_plane/artifact_member_quota.py`; Modify `herdr_engineering/control_plane/artifact_uploads.py` (`create_upload` takes `reserve_for`, `_finish_upload` releases); Test `tests/control_plane/test_artifact_member_quota.py`.

**Interfaces:**
- Consumes: `MemberStore`, `artifact_member_usage` (Task 1); `ArtifactUploadMixin`.
- Counting rule (pin; see Q4): `bytes_used` = sum of `size_bytes` of distinct `artifact_blobs` in the scope of the member's artifacts (all versions; content-addressed dedup within an artifact only). Per-artifact cap applies to one version's output manifest total.
- Produces: `class QuotaExceeded(ArtifactError)` with `.code` in `member_bytes`, `member_artifacts`, `artifact_bytes`; `async def reserve(conn, principal: str, nbytes: int) -> None` (`SELECT … FOR UPDATE` on the usage row; refuses if `bytes_used + bytes_reserved + nbytes > max_member_bytes`); `async def release(conn, principal, nbytes) -> None` (never below 0); `async def check_new_artifact(conn, principal) -> None` (count active artifacts under the same row lock; `member_artifacts`); `async def check_version(conn, principal, output_manifest: list[dict]) -> None` (`artifact_bytes`); `async def settle(conn, principal) -> int` (recompute `bytes_used` from blobs, called after each member commit, returns it). `create_upload(..., reserve_for: str | None = None)`: when set, reserves `size_bytes` in the same transaction as the session insert and stores `reserved_bytes/reserved_for`; `_finish_upload` releases the reservation exactly once (guard: `UPDATE … SET reserved_bytes=0 … RETURNING` the old value): on aborted/expired it is released; on **complete** it is moved into `bytes_used` under the same usage-row lock (the blob is already registered in the artifact's scope, so a completed-but-never-published upload still counts).

- [ ] **Step 1: Write failing tests**: reserve within limits OK; over `max_member_bytes` → `member_bytes`; **two concurrent `create_upload(reserve_for=…)` of 120 MiB each against a 200 MiB cap (two connections, `asyncio.gather`) → exactly one succeeds**; aborting releases; expiring via `expire_uploads` releases; completing moves the reservation into `bytes_used` at once (no publish needed); **open → complete repeated without ever publishing until the cap → the next open is refused `member_bytes`**; `settle` after a commit agrees with the incremental total; double abort releases once; 21st active artifact → `member_artifacts`, also under two concurrent creators at 19; a version manifest of 25 MiB + 1 B → `artifact_bytes`; uploads without `reserve_for` behave exactly as before (existing upload tests pass).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_member_quota.py tests/control_plane -k upload -q` → new FAIL.
- [ ] **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(artifacts): member quota reservations`.

### Task 4: Worker intent handlers for member artifacts

**Files:** Create `herdr_engineering/control_plane/artifact_member_intents.py`; Modify the plan-3 worker wiring (where `IntentLoop` handlers are registered) to register these kinds; Test `tests/control_plane/test_artifact_member_intents.py`.

**Interfaces:**
- Consumes: plan-3 `IntentLoop.register`, `IntentContext`; Tasks 1–3; `ArtifactStore.register_logical`, `commit_version`, `put_upload_part`, `complete_upload`, `abort_upload`.
- Every handler first calls `_member(ctx) -> str` (principal): `member_for_agent(ctx.agent, group=…, directory=…)`; not ok → result `{"status":"rejected","code":reason}` + audit; `usage.sharing_disabled` blocks share kinds only. Then ownership: `_own(ctx, principal, artifact_id)` = `created_by == principal`, else `{"status":"rejected","code":"not_found"}` (non-disclosing). Every outcome is recorded with `MemberStore.record` and `last_used_at` updated.
- Produces (intent `kind` → payload → result):
  * `member.artifact_create` `{title, filename, profile ∈ {"web","document"}}` → `check_new_artifact`, `register_logical(created_by=principal, artifact_type=…)` → `{"status":"created","artifact_id"}`.
  * `member.upload_open` `{artifact_id, filename, media_type, size_bytes, sha256}` → media type allowed (`text/html`, `application/pdf`, `application/vnd.apache.parquet`, `text/css`, `application/javascript`, `image/png|jpeg|svg+xml`, `application/json`), `size_bytes ≤ max_artifact_bytes`, `create_upload(part_size=524288, reserve_for=principal, ttl_seconds=3600)` → `{"status":"open","upload_id","part_count"}`.
  * `member.upload_part` `{artifact_id, upload_id, part_number, sha256, staged: "<relative path on intents volume>"}` → read staged bytes with `O_NOFOLLOW` beneath the uploads dir, `put_upload_part(..., sha256=…)`, delete the staged file → `{"status":"stored"}`.
  * `member.upload_complete` / `member.upload_abort` `{artifact_id, upload_id}` → `complete_upload`/`abort_upload`.
  * `member.publish` `{artifact_id, base_version_id|null, files: [{name, upload_id}], message, entry?}` → each upload complete and owned by this artifact; manifest names normalised with `share_bundle.normalise_name`/`check_names`; `check_version`; must contain an `.html` or `.pdf`; `commit_version(created_by=principal, base_version_id=…, provenance={"via":"member_mcp","agent_id":…})` (stale base → `{"status":"conflict","head_version_id"}`, no merge for members in v1); `settle` → `{"status":"created","version_id","version_number"}`.
  * Share kinds are Task 7.
- Produces the members' read model (plan 3's mirror holds only granted artifacts, so it cannot serve this): `async def write_member_view(store, share_store, member_store, principal: str, root: Path) -> None` writing `member-views/<sha256(principal)[:32]>.json` atomically (tmp + rename, 0640) on the read-only mount into `collab-public`: `{"principal", "updated", "usage": {bytes_used, bytes_reserved, max_artifact_bytes, max_member_bytes, max_artifacts, sharing_disabled}, "artifacts": [{artifact_id, title, lifecycle, head_version_id, head_version_number, bytes, live_shares, versions: [{version_id, version_number, created_at, message, parent_version_ids}]}]}`. One file per member, never containing another member's rows; rewritten after every applied member intent and by a worker pass every 60 s.

- [ ] **Step 1: Write failing tests** (fake `IntentContext` over the Postgres + fake RustFS fixtures): external agent → `external_agent`; agent whose owner left the group → `not_in_group`, nothing created; create → artifact with `created_by="member:alice"`; 21st create refused; upload open → part(s) → complete → publish produces a ready version whose outputs verify; a part whose bytes do not match its sha256 → rejected, staged file deleted; staged path `../../etc/passwd` or a symlink → rejected; publish naming another member's `upload_id` or `artifact_id` → `not_found`; publish with only `.css` files → refused; publish with a stale base → `conflict` carrying the head id; publish over 25 MiB → `artifact_bytes`; a refused intent writes one `artifact_member_events` row with outcome `refused`; after create and publish, alice's member-view file lists the artifact with its head and usage, and bob's file (also present) does not mention it.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(artifacts): worker applies member artifact intents`.

### Task 5: Member feed and MCP body limit for chunks

**Files:** Modify `herdr_engineering/control_plane/collab.py` (feed writer next to plan 3's `agents.json`), `herdr_engineering/control_plane/api.py` (`_jsonrpc`); Test `tests/control_plane/test_collab_member_feed.py`, `tests/control_plane/test_collab_mcp_upload_body.py`.

**Interfaces:**
- Produces: `CollabService.member_directory() -> dict` and a `member-directory.json` feed file beside `agents.json`: `{"group": str, "updated": float, "users": {user_id: {"email": str | None, "display_name": str, "status": "approved"|"disabled"}}}` — only users in the configured group; the worker loads it as `directory` for `member_for_agent` and as `member_emails` for Task 2. `CollabService.is_member_agent(pr) -> bool` (same rules as `member_for_agent`, evaluated on the collab side for the menu and the body limit).
- `_jsonrpc`: if `length > MAX_BODY`, authenticate from headers **before reading the body**; allow up to `MCP_UPLOAD_MAX_BODY = 768 * 1024` only when `is_member_agent(pr)`, else 413 without reading. After parsing, a body over `MAX_BODY` whose `params.name != "artifact_upload_chunk"` is answered with a JSON-RPC error `-32602 "request too large for this tool"`.

- [ ] **Step 1: Write failing tests**: feed lists only group users with email and status; a disabled user appears as `disabled`; an anonymous 700 KiB POST → 413 and the body is not read (handler reads 0 bytes); an internal non-member agent 700 KiB → 413; a member agent 700 KiB `artifact_upload_chunk` → accepted; a member agent 700 KiB `room_post` → `-32602`; 800 KiB → 413 for everyone.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** plus `tests/control_plane -k "collab_mcp or jsonrpc"` → PASS. **Step 5: Commit** `feat(collab): member directory feed and chunk-sized MCP bodies`.

### Task 6: Member MCP tools (create, upload, publish, list, history)

**Files:** Modify `herdr_engineering/control_plane/collab_mcp.py`; Create `herdr_engineering/control_plane/collab_member_tools.py` (staging + intent calls, keeps `collab_mcp.py` small); Test `tests/control_plane/test_collab_member_tools.py`.

**Interfaces:**
- Consumes: plan-3 `append_intent`/`read_result`; `is_member_agent` (Task 5); intent kinds (Task 4).
- Produces: `_MEMBER_ARTIFACT_TOOLS` (all `additionalProperties: False`), offered by `tools_for` only when `is_member_agent(pr)`; `_call` refuses them for anyone else with `CollabError(403, "artifact tools are for group members")`:
  * `artifact_create {title, filename, profile}`; `artifact_upload_open {artifact_id, filename, media_type, size_bytes, sha256}`; `artifact_upload_chunk {artifact_id, upload_id, part_number, sha256, data_base64}`; `artifact_upload_complete {artifact_id, upload_id}`; `artifact_upload_abort {artifact_id, upload_id}`; `artifact_publish {artifact_id, base_version_id?, files: [{name, upload_id}] (maxItems 200), message (≤ 500), entry?}`; `artifact_result {intent_id, seconds? 1–20}`; `artifacts_mine {}` (the members' "My artifacts": the caller's own member-view file from Task 4, read by the path derived from the caller's own principal only — never a caller-supplied path).
  * `artifact_head` / `artifact_history` (plan 3 tools) additionally answer for an artifact listed in the caller's own member-view file (that file is the ownership check on the collab side; the worker re-checks on every intent).
- Refusals decided in `collab-public` (wrong caller, bad arguments, public audience, backlog full) are audited through the collab event log (`CollabService._audit_denied`), since `collab-public` has no database; `artifact_member_events` holds only what the worker decides.
- `artifact_upload_chunk` decodes base64 (strict), checks decoded length ≤ 524_288 and sha256, writes `uploads/<agent_id>/<upload_id>/<part:05d>` with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600, then appends a `member.upload_part` intent. Per-agent staged-bytes cap `MEMBER_STAGED_MAX = 32 MiB` and volume-wide `STAGED_TOTAL_MAX = 512 MiB` (sum of directory sizes, checked before write → `CollabError(429, "upload backlog full; wait and retry")`). Rate limit via `service._throttle(f"member-artifact:{pr.id}", 600, 60.0, "artifact upload")` for chunks and `(…, 30, 60.0, …)` for the other writes.
- Mutating tools return `{"intent_id", "status": "pending"}` after waiting up to 5 s with `read_result` (so most calls return the final result); `artifact_result` polls longer. Results from the worker are returned untrusted-wrapped only where they contain member-authored text (`artifacts_mine` titles).

- [ ] **Step 1: Write failing tests**: menu for an internal member agent contains the tools; for an external agent and for an internal agent whose owner is not in the group it does not, and calling them returns 403; unknown argument → 400; bad base64 / wrong sha256 / 524_289-byte part → 400, nothing staged; staged file is 0600 under the agent's own dir; 33rd MiB staged → 429; chunk for another agent's `upload_id` is staged under the caller's dir only and the worker rejects it (`not_found`, via Task 4 fake loop); `artifacts_mine` returns only the caller's view file even when another member's file sits next to it; `artifact_history` on someone else's artifact → same error text as a nonexistent id.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** plus existing `test_collab_mcp*.py` → PASS. **Step 5: Commit** `feat(collab): member artifact MCP tools with chunked upload`.

### Task 7: Member share tools and the read-once URL

**Files:** Modify `herdr_engineering/control_plane/artifact_member_intents.py`, `collab_member_tools.py`, `collab_mcp.py`; Test `tests/control_plane/test_member_share_flow.py`.

**Interfaces:**
- Consumes: `ShareStore.create(…, member_groups, member_emails)` (Task 2), `approve_share` from `artifact_share_api`, `IntentContext.write_secret_result`.
- MCP tools (schema `audience: {"enum": ["lab"]}`; the tool also rejects any other value before queuing and records a refusal — defense in depth; the store is the authority): `artifact_share_create {artifact_id, version_id, allow_groups?, allow_emails?, expires_in_days? 1–3650, entry?, title? ≤ 240, audience?}` → summary (files, sizes, warnings, exactly as `_summary`); `artifact_share_approve {artifact_id, share_id, confirm: true, idempotency_key}` → `{"url"}` once; `artifact_share_list {artifact_id}`; `artifact_share_revoke {artifact_id, share_id}`; `artifact_share_events {artifact_id, share_id}`.
- Intent kinds: `member.share_create` (blocked when `sharing_disabled`; passes `member_groups=frozenset({group})`, `member_emails` from the directory; the member's display name reaches the lab viewer via Task 8's `shared_by`, never via the title), `member.share_approve` (calls `approve_share(server, share, approved_by=principal)` through `asyncio.to_thread` — the store's sync bridge must not be driven from the worker's own loop — and writes the URL with `write_secret_result`, the normal result carries only `{"status":"live","share_id","secret_result": true}`), `member.share_list|revoke|events` (ownership-checked; revoke reuses `_revoke` logic: state first, then row, then bundle).
- Read-once: the approve tool reads the secret result, deletes it, returns `{"url", "notice": "shown once; generate a new link to re-share"}`; a second read → `{"status":"already_read"}`. Replaying the same `idempotency_key` never calls `approve_share` again (the share is no longer pending → 409 mapped to `already_approved`).

- Global live-bytes cap across processes: `_APPROVE_LOCK` only serialises one process, and approvals now come from the API process and the worker. Add `ShareStore.spend_lock()` — an async context manager holding `pg_advisory_xact_lock(hashtext('artifact_share_live_bytes'))` in a transaction — and have `approve_share` re-read `live_total_bytes` and run `mark_live` inside it (keep `_APPROVE_LOCK` for the in-process directory work).

- [ ] **Step 1: Write failing tests**: two approvals — one via the HTTP route, one via the `member.share_approve` handler, on separate connections — that each fit but together exceed `max_total_bytes` → exactly one goes live, the other is refused with the total-cap message and leaves no bundle; end-to-end with fake loop: create (lab, group) → summary lists files → approve → URL once, second read `already_read`, secret file gone, URL absent from `artifact_member_events`, share events and every log record captured by `caplog`; **MCP `audience: "public"` → refused before queuing, one `_audit_denied` collab event, no intent file**; **a forged `member.share_create` intent with `audience: "public"` written directly to the intents dir → worker result `member_public_refused`, no share row**; `allow_groups: ["Local.Dev "]` (case/space variant of the group) → refused unless it normalises to exactly the group; email of a non-pool user → refused; sharing-disabled member → `sharing_disabled`; revoke of another member's share → `not_found`; secret result unread for 60 s is swept.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(collab): member lab sharing through MCP`.

### Task 8: "Shared by" banner on member lab links

**Files:** Modify `herdr_engineering/control_plane/artifact_share_api.py` (`approve_share` writes `shared_by` into `state.json` when the artifact owner is a member), `herdr_engineering/control_plane/share_public.py` (`_prepare` accepts optional `shared_by` ≤ 120 chars), `herdr_engineering/control_plane/share_static/viewer.html` + `share.js`; Test extend `tests/control_plane/test_share_lab.py`, `test_share_public.py`.

**Interfaces:** `state.json` gains optional `"shared_by": str` (member display name from the directory, else the user id). The lab viewer shell renders, when present, a non-dismissable banner `Shared by <name> — content was not reviewed by the operator`, text set via `textContent` only. Operator shares have no field and no banner.

- [ ] **Step 1: Write failing tests**: member approve → state has `shared_by`; operator approve → no key; a hostile name `<img src=x onerror=…>` renders as text; a state with `shared_by` over 120 chars or non-string is refused by `_prepare` (fail closed, 404 like any bad state).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `python3 -m pytest tests/control_plane/test_share_lab.py tests/control_plane/test_share_public.py -q` → PASS. **Step 5: Commit** `feat(shares): label member lab links`.

### Task 9: Operator controls — API, CLI, revocation on disable

**Files:** Create `herdr_engineering/control_plane/artifact_members_api.py`; Modify `herdr_engineering/control_plane/api.py` (route `/api/v1/artifact-members…`, operator-only), `herdr_engineering/artifact_shares_client.py` or a new `herdr_engineering/artifact_members_client.py` (CLI `herdr-eng artifacts members list|show|limits|disable-sharing|enable-sharing|revoke-share`); Test `tests/control_plane/test_artifact_members_api.py`, `tests/test_artifact_members_cli.py`.

**Interfaces:**
- Routes (all `_require("operator")`, same-origin POST check as share routes): `GET /api/v1/artifact-members` → `list_members()`; `GET /api/v1/artifact-members/{member}` → usage, artifacts (id, title, head version, bytes, lifecycle), shares (state, audience, recipients, expiry); `POST …/{member}/limits` `{max_artifact_bytes?, max_member_bytes?, max_artifacts?}`; `POST …/{member}/sharing` `{"enabled": bool}`; `POST …/{member}/shares/{sid}/revoke`. Disabling sharing revokes the member's live shares (default; Q3) and records events.
- Operator access to member artifacts (spec "the operator sees everything"): `_artifact_allowed` gains one rule — role `operator` has **`read` only** on artifacts with a `member:` owner. Share creation/approval on member content stays closed to the operator through the generic routes (non-disclosing 404); revoking a member's share is done only through the members route above. The open-listener mode already treats every caller as operator; that is the existing tailnet trust, unchanged.
- Worker: on each pass, members whose directory status is `disabled` or who left the group get their live shares revoked (`actor="member-disabled"`) — sweep function `async def sweep_disabled_members(store, share_store, directory, shares_root) -> int`.

- [ ] **Step 1: Write failing tests**: viewer-role key → 403 on every route; operator list shows usage and counts; limits validation (positive ints; `max_member_bytes` below current `bytes_used` accepted but blocks new reservations); disable sharing revokes live shares and later `member.share_create` → `sharing_disabled`; sweep revokes shares of a disabled directory user and is idempotent; operator key can `GET` a member artifact and its versions but `POST …/shares` on it → 404; CLI `herdr-eng artifacts share create --audience public` (and `--audience lab`) against a member-owned artifact exits non-zero and creates no row (operator key; the `member_public_refused` message itself is pinned by Task 2's store tests); CLI members commands print JSON.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(artifacts): operator member controls`.

### Task 10: Members panel (operator dashboard)

**Files:** Modify `packages/herdr-web-client/client.js` (`listArtifactMembers`, `getArtifactMember`, `setArtifactMemberLimits`, `setArtifactMemberSharing`, `revokeArtifactMemberShare`); Create `packages/artifact-members-panel.js`, `packages/artifact-members-panel.test.js`; Modify `packages/artifacts-view.js` (a "Members" section in the Artifacts view, shown only when `listArtifactMembers` succeeds — a 403 hides it); Create `tests/playwright/artifact-members.test.mjs` with a stub server in the style of `tests/playwright/artifact-shares-server.py` (plan 1).

**Interfaces:** `mountMembersPanel(container, client, { signal }) -> { dispose() }`; pure renderers `renderMemberRow(member)`, `renderMemberDetail(detail)` returning escaped HTML (`esc()`); quota bars show `bytes_used + bytes_reserved` of `max_member_bytes` and artifacts of `max_artifacts`; limits form in MiB; "Disable sharing" asks `confirm()` and states that live links are revoked; per-share Revoke.

- [ ] **Step 1: Write failing tests**: hostile display names/titles/emails escaped; quota bar values; disabled member shows "Sharing disabled"; no member → empty state text "No member artifacts yet"; Playwright: panel lists two members, set limits round-trips, disable sharing removes the live share rows, viewer key sees no Members section.
- [ ] **Step 2: Run** `node --test packages/artifact-members-panel.test.js` and the Playwright file → FAIL. **Step 3: Implement.** **Step 4: Run** `npm run test:client` + the Playwright file + UI gates for the touched surface (the `dashboard-missions__mobile` baseline is already red on main; not yours) → PASS. **Step 5: Commit** `feat(web): operator members panel`.

### Task 11: Documentation in this repo and abuse/security sweep

**Files:** Modify `packages/collab-guide.md` (+ its `docs/operations` mirror, regenerate `collab-guide.html` the way the repo does), `deploy/control-plane/fleet-config.example.yaml` (`control_plane.collab.members: {enabled: false, max_artifact_mib: 25, max_member_mib: 200, max_artifacts: 20}`; members tools stay off until `enabled: true`); Test `tests/control_plane/test_member_artifacts_security.py`.

**Interfaces:** `members.enabled: false` → `is_member_agent` always false and the worker refuses every `member.*` intent with `members_disabled`. Guide section "Publishing your own artifacts": tool list, quotas, lab-only rule, read-once URL, what the banner says.

- [ ] **Step 1: Write failing security tests** (one file, one test per line): member A cannot see, publish to, share, revoke, list shares of or read history of member B's artifact through any tool (same error as nonexistent); public refusal via MCP tool, forged intent, HTTP API on member artifact, CLI (Task 9 test reused by import), and `mark_live` on a SQL-inserted row; members switched off → tools absent and forged intents refused; an intent stamped 61 s ago → rejected; agent revoked between append and apply → rejected, staged chunks removed; 1,000 chunk calls in a minute → 429 after 600; config with `max_member_mib: 0` refused at load.
- [ ] **Step 2: Run** → FAIL where behaviour is missing; fix in the owning module. **Step 3: Update docs/config.** **Step 4: Run** `python3 -m pytest tests/control_plane -q -k "member or share or collab_mcp or upload"` → PASS. **Step 5: Commit** `docs(collab): member artifacts guide; security tests`.

### Task 12 (separate repo `berlinguyinca/herdr-artifacts`, `~/IdeaProjects/herdr-artifacts`): skill MCP mode

Do not edit that repo from this plan's branch; open a PR there. Describe-only here.

**Files (in herdr-artifacts):** `skill/herdr-artifact/scripts/herdr-artifact` (dispatch), new `skill/herdr-artifact/scripts/herdr-artifact-mcp` (Python 3 stdlib only), `skill/herdr-artifact/references/mcp-mode.md`, `SKILL.md`, `references/share-commands.md`, `install/install.sh` (no change to install paths), `tests/`.

**Interfaces:**
- Mode selection: MCP mode when `~/.config/herdr/artifact.yaml` has `mcp: {url, credential_file, device}` **and** `herdr-eng` is absent or its control plane is unreachable/unconfigured; `--mode mcp|cli` forces. No environment variables. The credential file is read, never echoed; it must be 0600 or the client refuses.
- Commands mapped to tools: `create` → `artifact_create`; `publish PATH… [--artifact ID] [--base VERSION] [--entry NAME] [-m MSG]` → hash each file, `artifact_upload_open`, 512 KiB `artifact_upload_chunk` loop with retry on 429/backoff and resume from the worker's missing-parts list, `artifact_upload_complete`, `artifact_publish`, poll `artifact_result`; `share ARTIFACT VERSION --group G | --email E… [--days N]` → `artifact_share_create`, print the exact file list and **stop for the human**; `share approve ARTIFACT SHARE` → `artifact_share_approve` (prints the URL once to stdout only); `list` → `artifacts_mine`; `revoke ARTIFACT SHARE` → `artifact_share_revoke`. `--audience public` in MCP mode exits 2 with "public links are operator-only" without calling the server.
- Docs: SKILL.md safety contract adds "members share to the lab group only; an agent must show its human the summary file list before approve; the link is shown once".

- [ ] **Step 1: Write failing tests** against a stub JSON-RPC server: chunking of a 1.3 MiB file → 3 chunks with correct sha256s; resume after an injected failure on chunk 2; 429 backoff; `--audience public` never reaches the server; credential file with mode 0644 refused; the approve URL appears on stdout only once and in no log file.
- [ ] **Step 2–4:** implement, run the repo's tests, verify each harness wrapper (Claude, Codex, Pi, OpenCode) calls the same script.
- [ ] **Step 5: Commit and open a PR in herdr-artifacts** `feat: MCP mode for member artifacts`; the herdr-engineering side needs no change for this.

---

## Self-review notes

* Spec E coverage: ownership via `created_by` + `member:` principal → T1; who is a member (group, disabled, external excluded) → T1/T5; tools `artifact_create/publish/share_*` + chunk upload → T6/T7; public never (server-enforced, audited, any caller) → T2 + T7 + T11; caps 25/200 MiB, 20 artifacts → T3/T4; content types, scanner, CSP, banner → T4/T8; intents queue, re-validation, rejected pending intents of disabled members → T4/T9/T11; skill MCP mode + guide → T11/T12; operator Members panel (list, revoke, quotas, disable) → T9/T10. Second-builder-check items: read-once URL file → T7; no member web UI (members get `artifacts_mine` instead) → T6, see Q5; reuse `artifact_acl`/`created_by`, `artifact_member_usage` → T1.
* Not in this plan: grant tools, mirror and intents queue themselves (plan 3), annotations (plan 4), merge for members (members' stale base is a plain conflict in v1).
* Names used across tasks: `member_principal`, `member_for_agent`, `MemberStore`, `QuotaExceeded`, `reserve/release/settle/check_new_artifact/check_version`, `member.*` intent kinds, `_MEMBER_ARTIFACT_TOOLS`, `is_member_agent`, `member-directory.json`, `write_member_view` / `member-views/<hash>.json`, `ShareStore.spend_lock`, `member_public_refused`, `member_audience_refused`, `MCP_UPLOAD_MAX_BODY` — consistent.

## Open questions for the operator

* **Q1** May the operator create links (public, or lab) for a member-owned artifact? Plan default: no — operator has read-only access to member content, a public row on it is refused at create and at go-live, and the operator would copy content into an operator artifact first.
* **Q2** How does a member free quota? Members have no delete/retract tool in the spec; plan adds none, so quota only grows until the operator acts.
* **Q3** When a member is disabled or sharing-disabled, are their live lab links revoked? Plan default: yes, automatically.
* **Q4** Does the 200 MiB count all stored versions (plan default, matches storage cost) or only heads?
* **Q5** The request asked for a "My artifacts" UI, the spec says members have no web UI. Plan delivers `artifacts_mine` (MCP/skill `list`) plus the operator Members panel; a member web page would need member sign-in to the dashboard.
