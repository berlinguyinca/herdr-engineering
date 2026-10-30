# Artifact collaborators over the collab MCP bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 3 of the artifact sharing / collaboration design. Collab agents that the owner grants an artifact read it and commit to it through six new tools on the collaboration MCP server. The Share panel gains Collaborators and Activity tabs.

**Architecture:** `collab-public` has no database or registry, so the bridge is three directories:
* **Mirror** (`grants.json`, `artifacts/<id>/head.json`, content-addressed `blobs/<sha256>`, `results/<ticket>.json`): written only by the worker and mounted read-only into `collab-public`.
* **Outbox** (`intents/<ticket>.json`, `agents.json`): written only by `collab-public` and consumed by the worker.

A new ~1 s worker loop with its own asyncpg connection re-checks everything against Postgres:
* the agent, from `agents.json`;
* the grant, from `artifact_acl` rows for `collab:<agent_id>`;
* retraction, from the durable state and a worker-local gate.

It stores the intent's text itself, calls the already-built `ArtifactStore.merge_commit` (in no outer transaction), records the outcome, writes the result file and refreshes the mirror. Owner routes on the dashboard manage grants and serve the Activity data. Grants are DB-only and never enter the web process's in-memory ACL.

**Tech Stack:** Python 3.12 / asyncpg (control plane, worker), stdlib file I/O (`os.open` + `O_NOFOLLOW`, rename-into-place), plain ES-module JS (`packages/`), node:test, Playwright, Postgres migrations in `deploy/control-plane/migrations`, docker compose overlays and nginx in `deploy/control-plane`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md`:
* section B (including the merge rules, scope and limits);
* section C;
* "Builder-check resolutions" and "Second builder check" (authority split, `collab:<agent_id>` ACL rows, 60 s intent age, `agents.json`, mirror = head + previous 5 versions' text files plus all manifests).

The merge engine (plan 2) is in `herdr_engineering/control_plane/artifact_merge.py` and `ArtifactStore.merge_commit` (`artifacts.py`). Its precedents:
* share-ingest: `artifact_shares.ShareStore.ingest_and_rotate_events`, `share_public.EventsWriter` / `_open_beneath`;
* MCP: `herdr_engineering/control_plane/collab_mcp.py`.

## Global Constraints

* **Principal and identity**
  * A collaborator's principal is exactly `collab:<agent_id>`. It is used as `created_by` for `merge_commit`, and the idempotency namespace follows from it. `collaborator_agent_id=<agent_id>`.
  * Grants are `artifact_acl` rows for `collab:<agent_id>`. Permissions are `["read"]` (read) or `["read","edit"]` (write), and `expires_at` is required. The default is 30 days and the maximum is 90.
  * A pending invite grant is never an `artifact_acl` row.
* **Tools and the MCP endpoint**
  * The six tools are `artifacts_list`, `artifact_head`, `artifact_read`, `artifact_history`, `artifact_commit` and `artifact_commit_result`.
  * Every tool is validated by hand (`additionalProperties: False` in the schema and an unknown-argument check in code).
  * Results that carry artifact content, history text or conflict text are wrapped `untrusted=True`.
  * `tools_for` offers the tools only to an agent with at least one live grant in `grants.json`.
* **Commit limits**
  * Writes are text files only (UTF-8): at most 100 changes and at most 2 MiB of text per commit (`len(text.encode())` summed).
  * Write grants are for plain file artifacts only: not `artifact_type == "report"`, no `report_descriptor` and no `source_manifest_blob` on the head. Read grants are allowed for any artifact.
  * Per-agent commit rate is 30/min (`CollabService._throttle`).
  * The daily byte quota per agent defaults to 50 MiB (`control_plane.collab_artifacts.daily_bytes_per_agent`).
  * At most 4 intents per agent may be pending in the outbox, and the outbox holds at most 64 MiB in total.
* **Intent validation in the worker**
  * The worker rejects an intent older than 60 s at first receipt (`submitted_at`).
  * It fails closed when `agents.json` is older than 120 s, and `collab-public` fails closed when `grants.json` is older than 120 s.
  * Intents carry `text` only. Any `sha256`, `storage_key`, `blob_sha256` or `entry` key in a change is rejected. Every name passes `share_bundle.normalise_name` on both sides.
* **Retraction**
  * A commit is refused if the artifact's `lifecycle == 'retracted'` (inside `merge_commit`), if the base or head version has `state = 'retracted'`, or if a worker-local `ArtifactGraphGate`, loaded with `load_retractions()` at least every 30 s, says the artifact, base or head is retracted.
  * The mirror never contains a retracted artifact or version.
* **`merge_commit` contract**
  * Always called without an outer transaction, so its `artifact_collab_attempts` row persists.
  * `ArtifactMergeConflict` → result `conflict`, carrying `head_version_id`, `{name, kind}` per file and `merged_text` (capped at 2 MiB in total per result; over the cap the file gets `merged_text: null, truncated: true`).
  * Any other `ArtifactError` → result `rejected` with its message.
* **No secrets anywhere.** Agent credentials, `Authorization` values, invite codes, share tokens and committed text never appear in intents (except the text itself), results (except conflict text), mirror metadata, worker/server logs or audit events. Logs name only the ticket, agent id, artifact id and outcome.
* **No public links from collaborators.** No tool, intent kind or route reachable by a `collab:` principal creates, approves or lists a share. The only intent kind is `commit`. Generic ACL grants refuse `collab:` and `collab-invite:` principals.
* **Audit**
  * Every commit submission and every denial goes through the collab event log.
  * Allowed reads are coalesced: at most one per (agent, artifact, tool) per 600 s, as `TERMINAL_AUDIT_EVERY` does.
  * The new event types join `BULK_EVENTS`.
* **Migrations.** Start at 0044 (0043 is this branch's). Re-check `git ls-tree origin/main deploy/control-plane/migrations/` right before committing and renumber if main has taken it. Migrations must be additive and idempotent.
* **Configuration and conventions**
  * Configuration is YAML only (`control_plane.collab_artifacts: {enabled, mirror_dir, outbox_dir, daily_bytes_per_agent}`; off unless `enabled` is exactly `true`). No environment variables are read by the code. Compose paths come from `deploy.sh` exports, as the share dirs do.
  * Web: everything goes through `esc()`, with no inline handlers and no tokens in storage. Owner POSTs are same-origin. Permission denials are non-disclosing 404s.
* **Commits.** Every commit ends with the attribution trailer the executing session's system reminder prescribes.

## Review Focus

* **Revocation mid-flight.** If the owner revokes a grant while an intent sits in the outbox (or the worker is mid-tick), the next tick must reject it (`rejected`, "no write grant"). Within ~2 s of the revoke, `artifacts_list` must stop listing the artifact. Test both, in Task 5 and in Task 8's end-to-end test.
* **Hostile names and text.** Commits containing `../x`, `a/../../b`, NUL, a CR in a name, a name that is a directory prefix of another file, 100 000 one-byte lines, and a very large single line must never escape the mirror, crash the applier or let a mirror read return a file outside `blobs/`. Pin with Task 3 (names) and Task 5 (applier).
* **Crash and retry.** A worker crash between `merge_commit` and the result write, or an agent resending the same `idempotency_key` with a new ticket, must yield exactly one version, and both tickets must report it. This must hold even when the restart comes more than 60 s after receipt (the `received` ledger row exempts it from the age check). Pin in Task 5.
* **One agent cannot see another's results.** Agent B guessing or copying agent A's ticket gets the same "not found" as an unknown ticket, including during the long-poll. Pin in Task 7.
* **Share panel tabs under failure.** If the collaborators route returns 503 (the bridge is disabled) or the agent list cannot be fetched (the collab overlay is off), the Links tab must keep working, and the Collaborators tab must say why without a broken form. Pin in Task 12.

---

### Task 1: Migration 0044 and the collaborator store

**Files:** Create `deploy/control-plane/migrations/0044_artifact_collab_bridge.sql`, `herdr_engineering/control_plane/artifact_collab_store.py`; Test `tests/control_plane/test_artifact_collab_store.py`. The test reuses the isolated Postgres fixture pattern of `test_artifact_version_parents.py`, applying migrations through 0044.

**Interfaces:**
* Migration:
  * `artifact_collab_intents(ticket_id text PK, artifact_id text NOT NULL, collaborator_agent_id text NOT NULL, base_version_id text, idempotency_key text, bytes bigint NOT NULL DEFAULT 0, outcome text NOT NULL CHECK (outcome IN ('received','created','merged','conflict','rejected')), version_id text, attempt_id text, reason text, received_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz)`.
    * No FK on `artifact_id`: an intent can name an artifact that does not exist.
    * Indexes on `(artifact_id, received_at DESC)` and `(collaborator_agent_id, received_at DESC)`.
  * `artifact_collab_invite_grants(invite_id text PK, artifact_id text NOT NULL REFERENCES artifacts, permission text NOT NULL CHECK (permission IN ('read','write')), expires_at timestamptz NOT NULL, granted_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), resolved_agent_id text, resolved_at timestamptz)`.
  * `ALTER TABLE artifact_collab_attempts ADD COLUMN IF NOT EXISTS ticket_id text`.
  * Include a `-- DOWN:` comment as 0043 does.
* `class CollabGrantStore(conn)`. Every method is `async`:
  * Grants:
    * `grant(artifact_id, agent_id, permission: Literal['read','write'], *, expires_at: datetime, granted_by: str) -> dict` upserts `artifact_acl` for `collab:<agent_id>`.
    * `revoke(artifact_id, agent_id) -> bool`.
    * `live_grants() -> list[dict]` returns `{artifact_id, agent_id, permission, expires_at}` for unexpired `collab:` rows.
    * `grant_for(artifact_id, agent_id) -> dict | None`.
  * Invite grants:
    * `add_invite_grant(invite_id, artifact_id, permission, *, expires_at, granted_by)`.
    * `resolve_invite_grants(join_to_agent: dict[str, dict]) -> int` creates the real grant with `expires_at = min(row.expires_at, agent.hard_expiry)` and marks the row resolved.
    * `pending_invite_grants(artifact_id) -> list[dict]`.
  * Intent ledger:
    * `record_received(ticket_id, artifact_id, agent_id, base_version_id, idempotency_key, bytes) -> dict | None` returns the existing row if the ticket is known.
    * `record_outcome(ticket_id, outcome, *, version_id=None, attempt_id=None, reason=None)`.
    * `bytes_last_24h(agent_id) -> int` (outcomes other than `rejected`).
  * Activity:
    * `collaborators(artifact_id) -> list[dict]` returns grants plus `last_used_at`, `commits`, `conflicts`, `rejected` from the ledger.
    * `attempts(artifact_id, *, limit=100) -> list[dict]` returns ledger rows with outcome `conflict`/`rejected`, joined to `artifact_collab_attempts.conflict_names` by `ticket_id`.

- [ ] **Step 1: Write failing tests:**
  * The migration applies twice.
  * `grant` writes `["read"]` or `["read","edit"]` with principal `collab:a1`, and `permission='admin'` raises `ValueError`.
  * `live_grants` drops expired rows.
  * `revoke` deletes the row.
  * `record_received` twice returns the first row.
  * `bytes_last_24h` sums only the last 24 h and excludes `rejected`.
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

### Task 3: Bridge file contract and safe I/O

**Files:** Create `herdr_engineering/control_plane/collab_bridge_files.py` (imported by both sides; stdlib only, no asyncpg); Test `tests/control_plane/test_collab_bridge_files.py`.

**Interfaces:**
* Constants:
  * Size and count limits: `MAX_CHANGES = 100`, `MAX_COMMIT_TEXT_BYTES = 2 * 1024 * 1024`, `MAX_INTENT_FILE_BYTES = 7 * 1024 * 1024`, `MAX_PENDING_PER_AGENT = 4`, `MAX_OUTBOX_BYTES = 64 * 1024 * 1024`, `MAX_MESSAGE = 1000`.
  * Why 7 MiB: it covers worst-case JSON escaping of 2 MiB of text. `validate_changes` refuses C0 control characters other than `\t`, `\n` and `\r` in text, so no character escapes to more than 2×: newline/quote/backslash-heavy text and a client's `ensure_ascii` CJK escapes are each at most 2×. The rest is headroom for the envelope.
  * Ages: `INTENT_MAX_AGE = 60.0`, `FEED_MAX_AGE = 120.0`.
  * Patterns: `TICKET_RE = r"ict_[0-9a-f]{24}\Z"`, `SHA_RE = r"[0-9a-f]{64}\Z"`.
* Safe I/O:
  * `read_json_beneath(root: Path, parts: list[str], *, max_bytes: int) -> dict | None`. It opens each component with `O_NOFOLLOW` through `dir_fd` (the `share_public._open_beneath` approach; extract and share it rather than copy if that stays small). It accepts regular files only, enforces the size cap before parsing and returns None on any failure.
  * `read_bytes_beneath(...) -> bytes | None` is the same check for blobs.
  * `atomic_write_json(dir: Path, name: str, obj: dict, *, mode=0o644)` serializes with `ensure_ascii=False`, writes `.tmp-<rand>`, fsyncs and `os.replace`s. It uses the share-bundle modes (files 0644, dirs 0755), so the reader container's uid can read what the other container's uid wrote.
  * `atomic_write_bytes(dir, name, data)` does the same for bytes.
* Validation:
  * `new_ticket() -> str`.
  * `validate_intent(raw: dict, *, filename: str) -> Intent` raises `IntentError(reason)`.
  * `@dataclass(frozen=True) class Intent: ticket: str; kind: Literal['commit']; agent_id: str; artifact_id: str; base_version_id: str; message: str; idempotency_key: str | None; changes: tuple[Change, ...]; submitted_at: float; text_bytes: int`.
  * `@dataclass(frozen=True) class Change: name: str; op: Literal['put','delete']; text: str | None`.
  * `validate_changes(changes: list) -> tuple[Change, ...]` is shared by `collab-public` and the worker. It runs `normalise_name` and `check_names` from `share_bundle`, requires `text` for a put and forbids it for a delete, rejects keys outside `{name, op, text}` (so `sha256`/`storage_key`/`entry` are refused), and enforces the caps.

- [ ] **Step 1: Write failing tests:**
  * A symlinked intent file, a symlinked parent dir, a FIFO and an oversize file all return None.
  * A filename and ticket mismatch raises, as do `kind: "share_create"` and an unknown top-level key.
  * Each of these change names raises: `../x`, `/abs`, a NUL, a CR, `a` together with `a/b`, and duplicates.
  * A change carrying `storage_key` raises, and so does one carrying `sha256`.
  * 101 changes raise; `MAX_COMMIT_TEXT_BYTES + 1` bytes raise.
  * Text containing `\x01` raises; `\t`, `\n` and `\r` are accepted.
  * A non-str text raises.
  * `atomic_write_json` never leaves a partial target. Simulate this by raising inside write: the target is unchanged and the tmp file is removed.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS, along with `tests/control_plane/test_share_public.py` if `_open_beneath` was extracted.
- [ ] **Step 5: Commit** `feat(collab): artifact bridge file contract`.

### Task 4: Mirror publisher (worker side)

**Files:** Create `herdr_engineering/control_plane/collab_mirror.py`; Test `tests/control_plane/test_collab_mirror.py` (Postgres fixture and a fake RustFS keyed by sha256).

**Interfaces:**
* Consumes `CollabGrantStore.live_grants` (Task 1), `ArtifactStore.list_versions`/`get_version`, `ArtifactGraphGate.load_retractions`, and `atomic_write_*` (Task 3).
* Produces `class CollabMirror(root: Path, *, history_versions: int = 5, max_manifests: int = 200, max_file_bytes: int = 2 MiB, max_artifact_bytes: int = 64 MiB, clock=time.time)` with:
  * `async publish_grants(grants, store, *, heartbeat: float = 30.0) -> bool`. It writes `grants.json = {generated_at, agents: {agent_id: {artifact_id: {permission, expires_at}}}}` at once when the content changed, and otherwise at least every `heartbeat` seconds (refreshing `generated_at`, so the 120 s freshness check in collab-public never trips while the worker is alive). It returns whether it wrote.
  * `async refresh(store, artifact_id, *, gate) -> dict`. It writes `artifacts/<artifact_id>/blobs/<sha256>` for the text files (UTF-8 decodable, ≤ `max_file_bytes`) of the head and the previous 5 non-retracted versions, skipping blobs that exist. It then writes `head.json` **last**:
    * `{artifact_id, title, artifact_type, writable: bool, writable_reason, head: {version_id, version_number}, versions: [{version_id, version_number, parent_version_ids, created_via, collaborator_agent_id, author_label, message, created_at, merge: bool, mirrored: bool, files: [{name, size_bytes, sha256, media_type, text: bool}]}]}`, newest first and capped at `max_manifests`.
    * `author_label` is `"owner"` or `"collaborator <agent_id>"` and never an email. `message` is `provenance.message`, capped at 1000 characters.
    * Unreferenced blobs are deleted after the swap.
  * `async remove(artifact_id)` deletes the artifact's mirror.
  * `async sync(store, *, gate, force: set[str] = frozenset())` publishes grants, refreshes every granted artifact whose head changed (or that is in `force`), and removes artifacts that have no live grant or are retracted.

- [ ] **Step 1: Write failing tests:**
  * A granted plain artifact gets `head.json` and blobs whose sha256 matches the content.
  * A binary or non-UTF-8 file is listed with `text: false` and has no blob.
  * A file over `max_file_bytes` is listed but not mirrored.
  * The 7th-newest version has `mirrored: false` and no blobs.
  * A report artifact has `writable: false, writable_reason: "report artifacts are read-only for collaborators"`.
  * A retracted version is absent from `versions`, and a retracted artifact's directory is removed.
  * Revoking the last grant removes the directory on the next `sync`.
  * `grants.json` is rewritten immediately on a grant change. When unchanged, it is not rewritten within 30 s but is rewritten (newer `generated_at`) after 30 s.
  * A crash simulated mid-refresh leaves the previous `head.json` intact.
  * No file in the mirror contains a `created_by` email: assert against a seeded `owner@example.org`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): read-only collaborator mirror`.

### Task 5: Intent applier (worker side)

**Files:** Create `herdr_engineering/control_plane/collab_intents.py`; Test `tests/control_plane/test_collab_intents.py` (Postgres fixture, fake RustFS, temp outbox and mirror).

**Interfaces:**
* Consumes Task 1's store, Task 3's `validate_intent`/`read_json_beneath`, Task 4's `CollabMirror.refresh`, and `ArtifactStore.merge_commit`/`register_report_blob`/`get_version`/`load_retractions`.
* Produces `class CollabIntentApplier(outbox: Path, mirror: CollabMirror, *, rustfs, daily_bytes_per_agent: int, lineage=None, clock=time.time, max_per_tick: int = 20)` with `async tick(conn) -> dict` returning `{applied, conflicts, rejected, skipped}`.
* Per intent file (oldest mtime first, names matching `TICKET_RE + ".json"`, regular files only):
  1. If the ledger has the ticket with a decided outcome, rewrite the result file from the row and unlink the intent.
  2. Validate the intent; an invalid one gets `rejected` with a fixed reason and no echo of content.
  3. If there is no ledger row and `now - submitted_at > INTENT_MAX_AGE`, reject `expired`. Otherwise `record_received`.
  4. `agents.json` (`read_json_beneath`, ≤ 4 MiB) must be fresh and must show the agent `active` with `expires_at > now`.
  5. `grant_for` must show `write`, unexpired.
  6. Check that the artifact is writable, and the gate plus version `state` for the base and the current head.
  7. Apply the daily quota.
  8. Store each put's text with `rustfs.put` and `register_report_blob(artifact_id, …)`. The entry is `{name, media_type: head entry's media_type or mimetypes.guess_type or "text/plain", sha256, size_bytes, storage_key}`.
  9. Call `merge_commit(created_by=f"collab:{agent}", collaborator_agent_id=agent, idempotency_key=intent.idempotency_key or ticket, …)` with no outer transaction.
  10. `record_outcome`, write `results/<ticket>.json = {ticket, agent_id, artifact_id, result, version_id?, version_number?, head_version_id?, merged_names?, conflicts?: [{name, kind, merged_text, truncated}], reason?, decided_at}`, unlink the intent, `mirror.refresh` on success and call `lineage(version)` (Task 6), whose errors are logged and never fail the tick.
* `sweep_results(max_age=3600)`.
* Log lines carry only ticket, agent, artifact and outcome.

- [ ] **Step 1: Write failing tests:**
  * **Outcomes:**
    * A fast-forward gives `created` with parents `[head]`.
    * A stale base with disjoint edits gives `merged` with parents `[head, base]`.
    * Overlapping edits give `conflict` with markers in `merged_text`, an unchanged head, one `artifact_collab_attempts` row with `ticket_id` and the ledger row `conflict`. This proves the attempt row persisted (no outer transaction).
  * **Refusals:**
    * No grant is `rejected` with "no write grant", and so is a read-only grant.
    * A grant revoked after the intent was written is `rejected`.
    * An expired grant is `rejected`.
    * An agent missing from `agents.json` or revoked there is `rejected`; a stale `agents.json` (> 120 s) is `rejected` with "agent feed stale".
    * An intent older than 60 s with no ledger row is `expired`.
  * **Crash and retry:**
    * An intent with a `received` row older than 60 s is still applied (crash recovery), and re-running the same file after a simulated crash before unlink yields the same version.
    * Two tickets with the same `idempotency_key` produce one version, and both results name it.
  * **Retraction and scope:**
    * An artifact retracted in the DB is `rejected`.
    * A retracted head version (`state`) is `rejected`.
    * A gate-only retraction loaded from `load_retractions` is `rejected`.
    * A report artifact is `rejected` with "not writable by collaboration".
  * **Quota and forged input:**
    * Exceeding the daily quota is `rejected`.
    * A forged change with `storage_key` pointing at another artifact's scoped blob is `rejected` by validation, and that blob is never referenced.
    * `kind: "share_create"` is `rejected`, no share row exists and the share module is never imported (assert `"herdr_engineering.control_plane.artifact_share_api" not in sys.modules` within a fresh subprocess run of the tick).
    * A symlinked intent file is skipped and never followed.
  * **No secrets in logs:** the committed text `SECRET-SENTINEL-…` never appears in `caplog`. The result file of a `rejected` intent does not contain the text.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_merge_commit.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): apply collaborator commit intents in the worker`.

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

### Task 7: collab-public artifact bridge (reads, intents, feed, audit)

**Files:** Create `herdr_engineering/control_plane/collab_artifacts.py`; Modify `herdr_engineering/control_plane/collab.py`:
* add the event types `artifact_tool_used`, `artifact_commit_submitted` and `artifact_tool_denied` to `BULK_EVENTS` and wherever `workspace_terminal_authorized` is handled in `_log_limit`/`_apply`;
* add an `artifact_bridge` attribute, None by default.

Test `tests/control_plane/test_collab_artifacts.py`.

**Interfaces:**
* `class CollabArtifactBridge(service: CollabService, mirror: Path, outbox: Path, *, clock=time.time)`. Each method takes an authenticated agent `Principal` and raises `CollabError`:
  * `has_grants(pr) -> bool` reads `grants.json`, which must be fresh, and checks the grant is unexpired. `has_write_grant(pr) -> bool` is the same check for a `write` grant on at least one artifact (Task 8 uses it).
  * `list(pr) -> dict` returns `{artifacts: [{artifact_id, title, permission, writable, head_version_id, head_version_number}]}`.
  * `head(pr, artifact_id) -> dict`.
  * `read(pr, artifact_id, name, *, version_id=None, offset=0, max_bytes=262144) -> {name, version_id, size_bytes, offset, text, next_offset}`.
    * `max_bytes` must be ≤ 1 MiB, and slices end on a UTF-8 boundary.
    * It verifies the blob's sha256 against the manifest.
    * An unmirrored version gives 404 "version not mirrored", and a binary file gives 415 "binary files are not readable through collaboration".
  * `history(pr, artifact_id, *, limit=50) -> dict`.
  * `commit(pr, args) -> {ticket, status: "pending"}`. It needs a `write` grant and `writable`.
    * Limits: `_throttle("artcommit:"+pr.id, 30, 60.0, "commit")`, the pending-per-agent and outbox-bytes caps, then `validate_changes` and an `atomic_write_json` of the intent.
    * It audits `artifact_commit_submitted` with ticket, artifact and byte count, never text. It records ticket→agent in a bounded in-memory map.
  * `result(pr, ticket, *, wait: float) -> dict`.
    * It checks ownership by the ticket map AND by the `agent_id` in the result file. A mismatch or an unknown ticket gives the same 404 "not found".
    * It polls the file at ≤ 0.5 s intervals up to `wait` (≤ 20 s) under the existing `MAX_WAITERS` cap and stops early if `service.still_valid(pr)` turns false.
    * It returns `{status: "pending"}` or the result.
  * `write_agents_feed() -> None` atomically writes `agents.json = {generated_at, agents: {agent_id: {owner, status, expires_at, external: bool, join_id, hard_expiry}}}`, with `join_id` from the reverse of `_joins_by_id`. `_housekeeping` is request-driven, so it cannot be the trigger. A daemon thread started with the bridge writes the feed every 15 s; it also runs on any agent revoke or disable, and inside `commit()` when the feed is older than 15 s, before the intent is written.
* Denials:
  * A missing grant, an ungranted artifact and an unknown artifact all give the same 404 "not found".
  * Denials go through `service._audit_denied(pr, "artifact.<tool>", reason, resource_id=artifact_id)`.
  * Allowed reads are audited `artifact_tool_used` at most once per 600 s per (agent, artifact, tool).

- [ ] **Step 1: Write failing tests** with a real `CollabService` and two agents (A granted write on X, B granted read on Y), a hand-written mirror and temp outbox:
  * **Listing and reads:**
    * A lists only X and B only Y.
    * A reading Y gets 404, the same as an unknown artifact, and the denial is audited.
    * `grants.json` older than 120 s makes `has_grants` False and every tool 503 "collaboration bridge unavailable".
    * A `read` slice never splits a multibyte character.
    * A blob whose sha256 differs from the manifest gives 502 "mirror integrity" and returns no text.
  * **Commits:**
    * B's `commit` on Y gives 403 "read-only grant".
    * The 31st commit within 60 s gives 429.
    * A 5th pending intent gives 429.
    * The intent file holds no `Authorization`, credential digest or device id.
    * With `agents.json` 200 s old and no request traffic, `commit()` refreshes the feed first, so the worker's freshness check passes.
  * **Results:**
    * B calling `result` with A's ticket gets 404, both immediately and after waiting.
    * `result` returns the worker's file content once it appears mid-wait.
  * **Audit and feed:**
    * 100 `read` calls produce one `artifact_tool_used` event.
    * `agents.json` lists the external agent's `join_id` and `hard_expiry`, and is rewritten after `revoke_agent`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** this test plus `tests/control_plane/test_collab_external.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): artifact bridge for collab-public`.

### Task 8: MCP tools, large commit bodies, and the end-to-end test

**Files:** Modify:
* `herdr_engineering/control_plane/collab_mcp.py`: `_ARTIFACT_TOOLS`, `ARTIFACT_TOOL_NAMES`, `tools_for`, `_call`;
* `herdr_engineering/control_plane/api.py`: `_jsonrpc`;
* `herdr_engineering/control_plane/collab.py`: `MAX_MCP_COMMIT_BODY`;
* `deploy/control-plane/collab-edge/nginx.conf`: the `/mcp` location.

Tests: `tests/control_plane/test_collab_mcp_artifacts.py`, `tests/e2e/test_artifact_collab_bridge.py`.

**Interfaces:**
* Tool schemas follow spec B's table. All of them have `additionalProperties: False`.
  * `artifacts_list {}`, `artifact_head {artifact_id}`, `artifact_read {artifact_id, name, version_id?, offset?, max_bytes? (1..1048576)}`, `artifact_history {artifact_id, limit? (1..200)}`.
  * `artifact_commit {artifact_id, base_version_id, message (≤1000), changes (array ≤100 of {name, op: put|delete, text?}), idempotency_key?}`.
  * `artifact_commit_result {ticket, seconds? (0..20)}`.
* `tools_for(service, pr)` appends `_ARTIFACT_TOOLS` only when `service.artifact_bridge` is set and `artifact_bridge.has_grants(pr)` holds. `_call` raises 404 "unknown tool" for those names otherwise.
* Wrapping:
  * Contents, history and results are wrapped `untrusted=True`.
  * The `head` and `list` manifests are wrapped `untrusted=True` because they carry names chosen by others.
  * The commit ticket reply is not wrapped.
* Request bodies in `_jsonrpc`, for label `"MCP"` only:
  * A `Content-Length` ≤ `MAX_BODY` behaves as today.
  * For `MAX_BODY` < length ≤ `MAX_MCP_COMMIT_BODY` (7 MiB, the same worst-case-escaping figure as `MAX_INTENT_FILE_BYTES`), it **authenticates first** (`service.authenticate` from headers). It reads the body only for an agent holding at least one `write` grant (`artifact_bridge.has_write_grant(pr)`). Otherwise it answers 413 without reading **and with `Connection: close`**, so unread bytes are never parsed as a next request (the share routes' precedent).
  * The binding limit is the 2 MiB text cap: a commit of ≤ 2 MiB of text always fits both the body cap and the intent cap.
  * nginx sets `client_max_body_size 7m` in `location = /mcp` only. The server-level 64k stays for every other route.

- [ ] **Step 1: Write failing tests:**
  * **Tool listing:**
    * An agent without grants gets a `tools/list` with none of the six tools, and `tools/call artifacts_list` gives the error "unknown tool".
    * A granted agent gets all six.
    * No tool name in any agent's list contains `share`, `acl` or `grant`, for internal and external agents with every grant combination.
  * **Arguments and wrapping:**
    * An unknown argument gives 400 for every tool.
    * `artifact_read` output starts with the `UNTRUSTED` notice.
  * **Body limits:**
    * A 2.5 MiB body from an unauthenticated caller gives 413 with `Connection: close`, and the body is never read: assert with an `rfile` that raises if read past 0 bytes.
    * Exactly 2 MiB of newline/quote-heavy text, and exactly 2 MiB of CJK text, are each accepted end to end, and the worker does not refuse the intent file. 2 MiB + 1 byte is refused with the text-cap message, not with a 413.
    * The same body from an agent with only read grants gives 413.
    * From a write-granted agent it is accepted.
    * `tests/test_compose_render.py`, or the existing nginx config test, asserts the 7m limit appears only in `/mcp`.
  * **End to end:** `test_artifact_collab_bridge.py` uses Postgres, a temp mirror/outbox, an in-process `CollabService` and `CollabArtifactBridge`, and drives `collab_mcp.handle` plus `CollabMirror.sync` and `CollabIntentApplier.tick`. It covers:
    * A grants write on X and the owner commits v2.
    * A reads v1 via MCP and commits a disjoint edit with base v1. A `merged` result arrives through `artifact_commit_result`, and `artifact_history` shows the merge with two parents.
    * A then commits an overlapping edit and gets `conflict` with markers, resolves it, and commits with `base = head_version_id` to get `created`.
    * The owner revokes. Within 2 ticks `artifacts_list` is empty, and a commit is refused 404 in collab-public, or `rejected` if it was already queued.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** both tests plus the existing MCP tests (`tests/control_plane/test_collab_external.py`, `test_collab_pentest.py`, `test_collab_bounded.py`) and expect PASS.
- [ ] **Step 5: Commit** `feat(collab): artifact collaboration MCP tools`.

### Task 9: Wiring, deployment and the fast worker loop

**Files:** Modify:
* `herdr_engineering/control_plane/worker.py`: add `CollabBridgeLoop`;
* `herdr_engineering/cli.py`: `_collab_artifacts_settings(cp)`, `_cmd_cp_worker` (start the loop), and the collab-only server flags `--collab-artifacts-mirror` / `--collab-artifacts-outbox` that construct `CollabArtifactBridge`;
* `deploy/control-plane/compose.yaml`: worker mounts;
* `deploy/control-plane/compose.collab.yaml`: `collab-public` mounts and flags;
* `deploy/control-plane/deploy.sh`: export and create `HERDR_COLLAB_MIRROR_DIR` and `HERDR_COLLAB_OUTBOX_DIR`, as for the share dirs;
* `deploy/control-plane/fleet-config.example.yaml`: `control_plane.collab_artifacts`.

Tests: `tests/control_plane/test_worker.py` (extend), `tests/test_compose_render.py` / `tests/test_share_deploy_config.py` (extend).

**Interfaces:**
* `CollabBridgeLoop(dsn_connect: Callable[[], Awaitable[Connection]], mirror: CollabMirror, applier: CollabIntentApplier, *, interval=1.0, mirror_every=30.0, retractions_every=30.0)` with `async run(stop: asyncio.Event)`.
  * It opens **its own** connection; it never uses the worker pass's connection.
  * Each tick runs `publish_grants`, the `applier.tick`, `resolve_invite_grants` from `agents.json` (Task 1), the throttled `mirror.sync`, the `load_retractions` refresh and `sweep_results`.
  * An exception is logged and the loop reconnects after 5 s.
* `_cmd_cp_worker` runs it as a separate `asyncio` task beside `run_worker` when `collab_artifacts.enabled is True`.
* Mounts:
  * The worker mounts `/collab-mirror` rw and `/collab-outbox` rw.
  * `collab-public` mounts `/collab-mirror:ro` and `/collab-outbox` rw.
  * Neither sees the other's private volumes. `collab-public` still has no Postgres or RustFS.

- [ ] **Step 1: Write failing tests:**
  * The worker loop uses a different connection object than `run_worker` (injected fakes).
  * A failing tick does not stop the loop.
  * With `enabled` absent or `false`, no loop starts and `collab-public` starts no bridge.
  * The compose render keeps the mount table in step: `collab-mirror` is `:ro` in `collab-public` and rw in the worker, and the `outbox` mount is present in both. `collab-public` still has no `env_file`, no Postgres network and no new capabilities.
  * `deploy.sh` creates both dirs owned by `HERDR_COLLAB_CERT_UID` with mode 0755, as it does for the share dirs.
  * The worker (which has no `user:`, so it runs as root) writes mirror files 0644 and dirs 0755, and can unlink in the outbox. A deploy test pins these modes.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the extended tests plus `tests/test_deploy_scripts.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(deploy): collaborator bridge directories and the 1 s worker loop`.

### Task 10: Owner routes for collaborators, activity and version diffs

**Files:** Create `herdr_engineering/control_plane/artifact_collab_api.py` (hook `route_collab` into `_route_api` beside `route_share`); Modify `herdr_engineering/control_plane/artifacts.py` (`list_version_summaries` adds `provenance->>'message' AS message`; add `version_text_diff`); Test `tests/control_plane/test_artifact_collab_api.py`.

**Interfaces:** All routes need `admin` on the artifact (via `_access`), except `diff`, which needs `read`. Denials are a non-disclosing 404. A POST with a foreign `Origin` gets 403. When `collab_artifacts.enabled` is not true, every route answers 503 "collaboration is not enabled".
* `GET /api/v1/artifacts/{id}/collaborators` returns `{writable, writable_reason, collaborators: CollabGrantStore.collaborators(id), pending_invites: [...]}`.
* `POST …/collaborators` takes `{agent_id, permission: read|write, expires_in_days: 1..90 = 30}` and returns 201.
  * `write` on a non-writable artifact gives 409 with `writable_reason`.
  * An `agent_id` must match `^[A-Za-z0-9_.:-]{1,80}$`.
  * An artifact without `created_by` gives 409 "legacy artifact: set an owner first".
* `POST …/collaborators/{agent_id}/revoke` returns 200.
* `POST …/collaborators/invite-grant` takes `{invite_id, permission, expires_in_days}` and returns 201. It stores a pending invite grant; the GUI calls it right after creating the invite through the existing `/api/v1/collab/rooms/{rid}/externals/invites` proxy.
* `GET …/collab-activity` returns `{attempts: CollabGrantStore.attempts(id)}`.
* `GET …/versions/{vid}/diff` returns `{version_id, against, files: [{name, change: added|deleted|modified|unchanged, diff: str|null, reason?: binary|oversize}]}`.
  * `against` is the version's first parent.
  * The `diff` is `difflib.unified_diff` text, capped at 2 MiB per file and 4 MiB in total, then `reason: oversize`.
  * Implemented as `ArtifactStore.version_text_diff(artifact_id, version_id) -> dict`.

- [ ] **Step 1: Write failing tests:**
  * A non-admin caller gets 404 on every route; a cross-origin POST gets 403.
  * A grant writes the `collab:` row and the web's in-memory `server.artifact_acl` is unchanged.
  * `write` on a report gives 409; `expires_in_days: 91` gives 400; an unknown body key gives 400.
  * Revoke removes the row.
  * Activity lists a conflict with its names.
  * The diff of a merge version is against its first parent, and a binary file gets `reason: binary`.
  * A hostile file name round-trips as data (JSON), never interpreted.
  * Version summaries include `message`.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** the new tests plus `test_artifact_share_api.py` and expect PASS.
- [ ] **Step 5: Commit** `feat(artifacts): owner routes for collaborators and activity`.

### Task 11: Owner CLI

**Files:** Create `herdr_engineering/artifact_collab_client.py` with `add_collab_subparser(asub)`, registered in `cli.py` after `add_share_subparser`; Test `tests/test_artifact_collab_client.py` (follow the share client tests).

**Interfaces:** These call Task 10's routes with the CLI's existing API-key client and print JSON:
* `herdr-eng artifacts collab list <artifact_id>`;
* `… grant <artifact_id> <agent_id> [--write] [--days N]`;
* `… revoke <artifact_id> <agent_id>`.

- [ ] **Step 1: Write failing tests:** each command hits the exact path, method and body, and `--days 0` is refused locally. **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS. **Step 5: Commit** `feat(cli): artifacts collab grant/list/revoke`.

### Task 12: Share panel Collaborators and Activity tabs

**Files:** Modify:
* `packages/herdr-web-client/client.js`: add the methods;
* `packages/artifact-share-panel.js`: tabs Links | Collaborators | Activity, where the Links tab is today's content unchanged.

Create `packages/artifact-collaborators.js` and `packages/artifact-activity.js`. Tests: `packages/herdr-web-client/artifact-collab.test.js`, `packages/artifact-collaborators.test.js`, `packages/artifact-activity.test.js`.

**Interfaces:**
* Client methods (all `{signal} = {}`):
  * `listArtifactCollaborators(id)`, `grantArtifactCollaborator(id, {agent_id, permission, expires_in_days})`, `revokeArtifactCollaborator(id, agentId)`, `grantArtifactInvite(id, {invite_id, permission, expires_in_days})`, `artifactCollabActivity(id)`, `artifactVersionDiff(id, versionId)`;
  * `listCollabAgents()` → `GET /api/v1/collab/agents`;
  * `createExternalInvite(roomId, body)` → the existing externals invites route.
* Renderers, pure, returning escaped HTML:
  * `renderCollaboratorRow(c, {busy})` shows agent, permission, expiry, last used, commits, conflicts and Revoke.
  * `renderGrantForm(state)` has an agent `<select>`, a Read/Read+write choice disabled with `writable_reason` when not writable, and days 1–90 (default 30).
  * `renderInviteResult(invite)` shows the code once, with the same once-only and clear-on-close rules as the share URL.
  * External invites are room-bound, so the invite form has a room `<select>` filled from the operator's rooms. Reuse the existing collab rooms client call that `collab-app.js` / `collab-view.js` use (`GET /api/v1/collab/rooms` through the dashboard proxy), adding a `listCollabRooms()` client method if none exists.
  * `renderHistoryRow(v)` shows number, author label, message, parents and a merge badge.
  * `renderAttempt(a)` shows outcome, agent, conflict names, reason and time.
* Mounting:
  * `mountCollaborators(container, client, artifactId, {signal})` and `mountActivity(container, client, artifactId, {versions, signal})`.
  * Activity's "Show rejected attempts" toggle loads lazily.
  * Selecting a version renders the file list and `renderDiff` (from `herdr-web-components/inspector.js`) for each modified file.

- [ ] **Step 1: Write failing tests:**
  * The client hits exact paths and bodies, with ids `encodeURIComponent`-ed.
  * A hostile agent name, message, file name or conflict name (`<img onerror>`) is escaped.
  * A non-writable artifact disables Read+write and shows the reason.
  * A 503 from `listArtifactCollaborators` shows "Collaboration is not enabled" with the form hidden, and the Links tab still works.
  * A failed `listCollabAgents` shows an agent-id text input instead of the picker.
  * The invite code disappears from the DOM on Close.
  * The rejected toggle fetches only once.
  * A merge version shows two parents and the badge.
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

**Files:** Modify `packages/collab-guide.md` and its mirror `docs/operations/collaboration-network-guide.md` (keep them identical; run any test that checks this), `skills/herdr-artifact/SKILL.md` (a short "collaborating through MCP" reference) and `deploy/control-plane/COLLAB.md` (bridge dirs, enable switch, rollback = `enabled: false`).

- [ ] **Step 1:** Document:
  * the six tools with arguments and results;
  * the commit/merge/conflict loop: resolve, then re-commit with `base = head_version_id`;
  * the limits (100 changes, 2 MiB, 30/min, 50 MiB/day, 60 s, text-only and plain-artifact-only writes);
  * the honest promise that revocation takes effect within seconds;
  * that collaborators can never share.
- [ ] **Step 2: Run** the docs or guide tests (`grep -rl collab-guide tests/`) and expect PASS. **Commit** `docs(collab): artifact collaboration tools`.

---

## Open questions for the operator

1. **Commit body size.** To carry the spec's 2 MiB of text per commit, this plan raises the public `/mcp` body cap from 64 KiB to 7 MiB. Only authenticated agents with a write grant get the large cap, and the edge cap applies to `/mcp` only. The alternative is a chunked staging tool, a seventh tool beyond the spec's six. Which one?
2. **Daily quota.** The default daily byte quota is 50 MiB per agent. Is that right?
3. **Retraction residual.** If a retraction is hidden in the web process but fails to persist, the worker still applies collaborator commits; only their lineage is refused. Is that acceptable, or should a failed persist also revoke `collab:` grants?
4. **Grant scope and lifetime.** May internal (non-external) agents receive write grants? Is 90 days the right maximum grant lifetime (default 30)?

## Self-review notes

* **Spec coverage:**
  * B's tools are covered by Tasks 7–8; the mirror by Task 4; the intents queue and fast loop by Tasks 3, 5 and 9; the worker as authority by Task 5.
  * `collab:` ACL rows: Tasks 1–2 and 10. Commit semantics and merge rules: plan 2's `merge_commit`, driven by Task 5. Scope (plain file writes only): Tasks 4, 5 and 10.
  * History model: plan 2, plus `message` in Task 10. Limits, audit and safety: Tasks 3, 5, 7 and 8. Owner revert: plan 1.
  * Docs: Task 14. CLI: Task 11. `agents.json`: Task 7, consumed in Tasks 5 and 9. The invite+grant step: Tasks 1, 7, 9, 10 and 12.
  * Section C is covered by Tasks 10, 12 and 13.
  * Out of scope: annotations (plan 4) and member-owned artifacts (plan 5).
* **Retraction:** the durable state and the worker-local gate are checked in Task 5; the web's in-memory gate is checked at lineage emission in Task 6. Residual: if the web hides an artifact but fails to persist the retraction (`retract` returns `persisted: false`, HTTP 500), the worker cannot see it. Collaborator commits keep landing until the operator retries, though their lineage is refused. This is called out for the operator, not hidden.
* **Name consistency:** `CollabGrantStore`, `CollabMirror`, `CollabIntentApplier`, `CollabArtifactBridge`, `CollabBridgeLoop`, `validate_intent`, `validate_changes`, `Intent`, `Change`, `TICKET_RE`, `INTENT_MAX_AGE`, `FEED_MAX_AGE`, `artifact_collab_intents`, `artifact_collab_invite_grants`, `control_plane.collab_artifacts`.
