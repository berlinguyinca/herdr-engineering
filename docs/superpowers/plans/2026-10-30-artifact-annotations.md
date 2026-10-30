# Artifact annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 4 of the artifact sharing / collaboration design. Signed-in lab viewers and the owner can annotate a lab-shared artifact. Each annotation is bound to the share's pinned version plus an anchor. The owner sees and dismisses annotations in a Feedback section of the Share card. A granted agent reads open annotations and marks them addressed through two MCP tools. Anonymous and public visitors get no annotation surface at all.

**Architecture:**
- Today the lab viewer navigates away from its shell (`share.js` `location.replace(base + entry)`), and every content response sends `frame-ancestors 'none'`. This plan therefore builds a real viewer shell first:
  - A new route `GET /s/<token>/k/<session>/` serves `viewer.html` in shell mode.
  - The shell frames `f/<entry>` in a sandboxed iframe.
  - Lab-session content may be framed by `'self'`.
- For HTML, `share-public` appends one `<script src=".../k/<session>/annotate.js">`, which reports selections to the shell by `postMessage`.
- The shell posts annotations to `POST /s/<token>/k/<session>/annotations`. This is the first route in `share-public` that reads a body: it is size-capped, needs the exact public `Origin` plus a custom header, has no preflight, and is rate-limited.
- `share-public` stays database-free. It appends to `annotations.jsonl`. The worker ingests that file into `artifact_annotations`, deriving artifact and version from the share row and never from the line. The worker then writes per-viewer read-back projections onto the shares volume and the open-annotation list into plan 3's MCP mirror.
- `artifact_annotation_resolve` is a plan-3 intent. The control-plane worker is the authority that applies it.

**Tech Stack:**
- Python 3.12 stdlib `http.server` (`share_public.py`), asyncpg (control plane and worker), plain ES-module JS (`packages/`, `share_static/`).
- Tests: node:test for unit tests, Playwright (Chromium) for UI, pytest with the Postgres fixtures of the existing `test_share_*` / `test_artifact_*` tests.
- Postgres migrations live in `deploy/control-plane/migrations`. nginx edge snippets live in `deploy/control-plane/collab-edge`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md`, section D, plus the "Second builder check" bullet "Owner annotations". Share-link mechanics: `docs/superpowers/specs/2026-10-30-artifact-share-links-design.md`. Code: `herdr_engineering/control_plane/{share_public,share_session,share_lab,artifact_shares,artifact_share_api,worker}.py`, `share_static/`.

## Consumed from plan 3 (`docs/superpowers/plans/2026-10-30-artifact-collaborators-mcp-bridge.md`)

Task 11 builds on these plan-3 names. If plan 3 changes them before it merges, follow plan 3.
- **Bridge files** (`collab_bridge_files.py`): `read_json_beneath`, `atomic_write_json`, `new_ticket`, `TICKET_RE`, `validate_intent(raw, *, filename) -> Intent`. Plan 3's `Intent.kind` is `Literal['commit']`, and plan 3 states "the only intent kind is `commit`". **This plan widens that to `'commit' | 'annotation_resolve'`.** It adds an `AnnotationResolveIntent` shape — `{ticket, kind, agent_id, artifact_id, version_id, annotation_ids, idempotency_key, submitted_at}` — and refuses unknown keys. `share_create` and every other kind are still refused.
- **Mirror** (`collab_mirror.py`, `CollabMirror`): the worker owns it and it is read-only in `collab-public`. The layout is `grants.json`, `artifacts/<id>/head.json`, `blobs/`, `results/<ticket>.json`. `sync()` removes the directory of an artifact that is ungranted or retracted.
- **Applier** (`collab_intents.py`, `CollabIntentApplier.tick`): runs the 60 s age check, the ledger replay, the `grant_for(artifact_id, agent_id)` write check (from `CollabGrantStore`), the result file and the mirror refresh. It dispatches on `kind`.
- **collab-public** (`collab_artifacts.py`, `CollabArtifactBridge`):
  - `has_grants(pr)` and the read path.
  - `result(pr, ticket, *, wait)`, which checks ticket ownership.
  - The pending-per-agent and outbox caps, and the ticket→agent map.
  - Denials go through `service._audit_denied(pr, "artifact.<tool>", reason, resource_id=...)`. Allowed reads are audited as `artifact_tool_used`.
- **MCP** (`collab_mcp.py`): `_ARTIFACT_TOOLS`, `ARTIFACT_TOOL_NAMES`, `tools_for` (offers the tools only when `has_grants`), `_call`. Throttling uses `CollabService._throttle(key, limit, window, what)`.

## Global Constraints

- **Who may annotate.**
  - Only a lab-share viewer holding a valid session minted from verified Cognito tokens that passed `share_lab.authorise`.
  - The owner annotates the same way, through the lab link.
  - Public shares and anonymous visitors have no annotation route. Every annotation path on a `public` share is the uniform 404.
- **Text is never markup.**
  - Annotation text is plain text everywhere: `textContent` in the viewer, `esc()` in the dashboard, JSON data with the `UNTRUSTED` notice (`untrusted=True`) in MCP.
  - It is never written with `innerHTML`, never interpolated into HTML by the server, and never logged.
- **Anchor kinds.** Validated identically in `share-public` and in the worker by one pure module.
  - `{"kind": "document", "file"}` (any file type)
  - `{"kind": "text_quote", "file", "exact", "prefix", "suffix"}` (HTML)
  - `{"kind": "pdf_page", "file", "page", "exact"?}` (PDF)
  - `{"kind": "image_region", "file", "x", "y", "w", "h"}` (images; see the open question in Task 8)
  - Parquet data views are not annotatable.
- **Caps.** One constant each in `annotation_anchor.py`:
  - request body ≤ 16 KiB
  - comment 1–4000 characters after strip; no C0 controls except `\n` and `\t`, no NUL, no surrogates
  - `exact` 1–1000 characters
  - `prefix` and `suffix` ≤ 64 characters each
  - `file` normalised by `share_bundle.normalise_name` and ≤ 1024 bytes
  - `page` 1–100000
  - region values are finite floats in [0, 1] with `x + w ≤ 1` and `y + h ≤ 1`
- **Rate limits.**
  - `share-public` (in-memory `RateLimiter`): per (share, identity) 10/min; per share 60/min; global 600/min.
  - Durable caps, enforced at worker ingest and surviving `share-public` restarts: per (share, author) 100/day; per share 1000 total.
  - Every refusal is a `denied` share event with a fixed reason code.
- **Annotations file.** `<events_dir>/annotations.jsonl` follows the same rules as `events.jsonl`: one `os.write` per line under `O_APPEND`, line ≤ 16 KiB, file cap `EVENTS_MAX_BYTES` (8 MiB), rotation past `EVENTS_ROTATE_BYTES` (4 MiB), idempotent ingest keyed by `sha256(line):offset`.
- **Version pinning.**
  - An annotation stores `artifact_id` and `version_id`, both taken from the `artifact_shares` row at ingest, plus its anchor.
  - A new revision never changes a live share link. Addressing an annotation only records `addressed_in_version_id`.
- **Configuration is YAML only** (no environment variables):
  - `control_plane.share.annotations`:
    - `enabled` (bool, default `true`)
    - `per_user_per_minute` (10), `per_share_per_minute` (60), `per_user_per_day` (100), `max_per_share` (1000)
    - Add `annotations` to `SHARE_SLICE_KEYS` so `share-public` can read it.
  - `control_plane.share.owner_emails` (list, default `[]`) is read by the API and the worker only. It is NOT a slice key, so the operator's email never reaches `share-public`.
- **Migration.** Number `0045`. 0043 is taken by `feat/artifact-collab-gui`, 0044 is plan 3's and 0046 is plan 5's. Re-check `origin/main` and open branches before committing and take the next free number. The migration must be additive and idempotent, with a `DOWN:` comment like 0043.
- **Never logged or stored outside the response that needs it:** tokens, sessions, `Authorization` and `X-Id-Token` values. Author emails reach only the owner API and the author's own projection. They never reach the MCP mirror or an agent.
- **Code style.** Python: `from __future__ import annotations`, type hints, the surrounding module's style. JS: ES modules, 2-space indent, the conventions of `artifact-share-panel.js` (`esc()`, `hd-*` classes, `data-*` test hooks, no inline handlers).
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Hostile bundle content cannot annotate as the viewer.** The iframe's own URL contains the session, and its `connect-src` allows `/s/<token>/`. A `fetch` POST from inside the content (Origin `null`, or no custom header, or an `OPTIONS` preflight) must be refused, with no line written. Pinned in Task 6.
- **Hostile annotation text** (`<img src=x onerror=…>`, `"><script>`, RTL override `‮`, zero-width characters, 4000 characters of one unbroken word) appears literally and wraps. It never executes in the viewer list, the Feedback panel or the event drawer, and in MCP it arrives inside the `UNTRUSTED` wrapper. Pinned in Tasks 8, 10, 11 and 12.
- **A session or share that dies mid-use.**
  - Revoke, expiry or session expiry between page load and submit → the POST gets the uniform 404, and the shell shows "Your session ended — open the link again" with the typed comment kept in the box.
  - A line written before the revoke is still ingested when its `at` precedes `revoked_at`; one after is refused.
  - Pinned in Tasks 4, 6 and 8.
- **Forged or replayed lines from a compromised or restarted `share-public`:**
  - unknown share, public share, file not in the pinned version, quota exceeded, duplicate `annotation_id`, an `artifact_id` or `version_id` field in the line (ignored)
  - Each is either refused with a `denied` event or ingested exactly once.
  - Pinned in Task 4.
- **Unusual entry content.** An HTML entry over the injection cap, a page with its own strict meta-CSP, or a frame-busting `top.location = …` script still displays. Frame-busting is blocked by the sandbox, which has no `allow-top-navigation`. A whole-file comment remains possible whenever selection capture is unavailable. Pinned in Tasks 5 and 12.

---

### Task 1: Anchor and comment validation (pure)

**Files:** Create `herdr_engineering/control_plane/annotation_anchor.py`; Test `tests/control_plane/test_annotation_anchor.py`

**Interfaces:**
- Produces:
  - Constants: `MAX_BODY_BYTES = 16384`, `MAX_COMMENT_CHARS = 4000`, `MAX_EXACT_CHARS = 1000`, `MAX_CONTEXT_CHARS = 64`, `MAX_PAGE = 100000`, `ANCHOR_KINDS = ("document", "text_quote", "pdf_page", "image_region")`.
  - `class AnnotationInputError(ValueError)` with `.code` in `bad_anchor | bad_file | empty | too_long | bad_chars`.
  - `def clean_comment(value: object) -> str`
  - `def clean_anchor(value: object) -> dict` returns a new dict holding only the allowed keys for its kind, `file` normalised.
  - `def parse_annotation_request(raw: bytes) -> tuple[dict, str]` parses `{"anchor": {...}, "comment": "..."}`. Unknown top-level keys → `bad_anchor`. Returns `(anchor, comment)`.
- Never raises anything but `AnnotationInputError`.

- [ ] **Step 1: Write failing tests**
  - Each kind accepted with its minimal and maximal fields.
  - Extra keys stripped inside the anchor but refused at top level.
  - Boundaries exact:
    - comment 4000 accepted, 4001 → `too_long`
    - `exact` 1000 / 1001
    - prefix 64 / 65
    - page 0, 1, 100000 and 100001
    - `True` as page refused
    - region with `x + w = 1.0000001` refused; NaN and inf refused
  - Comment `"  \n "` → `empty`. `"\x00"`, `"\x1b"` and `"\ud800"` → `bad_chars`. `"\n"` and `"\t"` kept.
  - `file` values `"../a"`, `"a//b"`, `"/abs"` and a 1025-byte name → `bad_file`.
  - Non-JSON, a JSON list, 16385 bytes → `bad_anchor` / `too_long`.
  - A hostile comment `<img src=x onerror=1>` is returned unchanged (stored as text, not filtered).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_annotation_anchor.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.** Reuse `share_bundle.normalise_name` for `file`. Parse with `json.loads` on the bytes after a length check.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(annotations): anchor and comment validation`.

### Task 2: Reusable JSONL ingestor (refactor, behaviour pinned)

**Files:** Create `herdr_engineering/control_plane/jsonl_ingest.py`; Modify `herdr_engineering/control_plane/artifact_shares.py` (`ShareStore.ingest_events_file` / `ingest_and_rotate_events` delegate); Test: existing `tests/control_plane/test_share_store.py`, `test_share_worker.py`, plus new `tests/control_plane/test_jsonl_ingest.py`

**Interfaces:**
- Produces:
  - `class JsonlIngestor` with `__init__(self, conn, *, parse_line: Callable[[bytes], Any | None], store: Callable[[Any, Any, str], Awaitable[bool]], max_line_bytes: int = 16384)`. Here `store(conn, parsed, ingest_key)` runs inside a per-line savepoint and returns True when a row was inserted.
  - `async def ingest_file(self, path: Path, marker: Path) -> int`
  - `async def ingest_and_rotate(self, path: Path, marker: Path, *, rotate_bytes: int, now: float, grace: float) -> int`
- The offset/fingerprint marker, the torn-tail rule, oversize-line skipping, the rotation to `.1`, the regular-file-only open and the poison-line skip move here unchanged from `ShareStore`.

- [ ] **Step 1: Write failing tests** in `test_jsonl_ingest.py` with an in-memory `store` callback (no Postgres):
  - A torn last line waits for its newline.
  - A replay after deleting the marker calls `store` with the same `ingest_key` values.
  - A symlink planted at `path` is ignored.
  - A `store` raising `ValueError` skips and consumes that line.
  - Rotation happens only once `rotate_bytes` are consumed, and the `.1` file is drained and deleted on the next call after `grace`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** by moving the code. `ShareStore` keeps its public method names and `_parse_line`, and passes an adapter closure `store(conn, parsed, key)` that unpacks the parsed tuple into `self._event(conn, share_id, kind, actor, detail, at, key)`.
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_jsonl_ingest.py tests/control_plane/test_share_store.py tests/control_plane/test_share_worker.py -q` → PASS, with no edits to the existing share tests.
- [ ] **Step 5: Commit** `refactor(shares): extract the JSONL ingestor`.

### Task 3: Migration 0045 and `AnnotationStore`

**Files:** Create `deploy/control-plane/migrations/0045_artifact_annotations.sql`, `herdr_engineering/control_plane/artifact_annotations.py`; Test `tests/control_plane/test_artifact_annotations_store.py` (Postgres fixtures as in `test_share_store.py`)

**Interfaces:**
- **Migration**, additive and idempotent:
  - `artifact_annotations` columns:
    - `annotation_id text PK CHECK (annotation_id ~ '^ann_[A-Za-z0-9_-]{22}$')`
    - `artifact_id text NOT NULL REFERENCES artifacts`
    - `version_id text NOT NULL REFERENCES artifact_versions`
    - `share_id text NOT NULL REFERENCES artifact_shares`
    - `file_name text NOT NULL`
    - `anchor jsonb NOT NULL`
    - `body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000)`
    - `author_identity text NOT NULL`
    - `author_email text NOT NULL`
    - `author_role text NOT NULL CHECK (author_role IN ('member','owner'))`
    - `status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','addressed','dismissed'))`
    - `addressed_in_version_id text REFERENCES artifact_versions`
    - `resolved_by text`
    - `resolved_at timestamptz`
    - `created_at timestamptz NOT NULL`
    - `ingested_at timestamptz NOT NULL DEFAULT now()`
    - `updated_at timestamptz NOT NULL DEFAULT now()`
    - `ingest_key text UNIQUE`
  - Table CHECK: `(status = 'addressed') = (addressed_in_version_id IS NOT NULL)`.
  - Indexes `(artifact_id, status, created_at DESC)`, `(share_id, author_identity, created_at DESC)`, `(updated_at)`.
  - Widen the `artifact_share_events.kind` CHECK to add `'annotated'`, `'annotation_dismissed'`, `'annotation_addressed'`. Drop and re-add the constraint by name inside a `DO` block that is a no-op when already widened.
- **Store** `AnnotationStore(conn)`:
  - `async def ingest(self, conn, record: dict, ingest_key: str, *, owner_emails: frozenset[str], per_user_per_day: int, max_per_share: int) -> str`. Returns `"inserted"`, `"duplicate"`, or a refusal code: `unknown_share`, `not_lab`, `not_live`, `file_not_in_version`, `quota_user_day`, `quota_share`, `invalid`.
    - A refusal also writes a `denied` share event with `{"reason": "annotation_" + code}`. A success writes an `annotated` event with `{"annotation_id", "file"}`.
    - `artifact_id` and `version_id` come from the share row. Fields of that name in `record` are ignored.
    - `file` must be one of the names the share actually exposes, from `shared_file_names(artifact_row, version_row) -> frozenset[str]`. This is a new helper in `artifact_share_api.py`, factored out of `_plan` and used by it: `{PREVIEW_NAME}` for a report artifact, otherwise the normalised `output_manifest` names. Report bundles contain only the synthesized `preview.html`, which is not in the manifest.
    - The share must have been live at `record["at"]` (`approved_at ≤ at < revoked_at`, or `revoked_at` NULL), and `at` is clamped to ≤ now + 5 min.
    - `author_role` is `owner` when the email is in `owner_emails`.
  - `async def list_for_artifact(self, artifact_id: str, *, status: str = "open", limit: int = 200) -> list[dict]`. Rows carry `version_number` and `addressed_in_version_number`. `status` takes `open | addressed | dismissed | all`; `limit` is 1–500.
  - `async def dismiss(self, artifact_id: str, annotation_id: str, *, actor: str) -> dict`. Raises `AnnotationError(code="not_found" | "not_open")`. Dismissing an already dismissed annotation returns the row unchanged.
  - `async def resolve(self, artifact_id: str, annotation_ids: list[str], *, version_id: str, actor: str) -> dict` → `{"resolved": [ids], "skipped": [{"annotation_id", "reason"}]}`.
    - Skip reasons: `not_found`, `not_open`, `version_not_newer`.
    - Refuses a `version_id` that is not of this artifact with `AnnotationError("bad_version")`.
    - Writes one `annotation_addressed` event per resolved row.
  - `async def own_rows(self, share_id: str) -> dict[str, list[dict]]` → author_identity → newest 200 rows. Fields: `annotation_id, file, anchor, body, status, version_number, addressed_in_version_number, created_at`.
  - `async def open_for_agents(self, artifact_id: str, limit: int = 200) -> list[dict]`. Fields: `annotation_id, version_id, version_number, file, anchor, body, author_role, created_at`. No email and no identity.
  - `async def touched_since(self, since: datetime) -> tuple[set[str], set[str]]` → (share ids, artifact ids) of rows with `updated_at > since`.

- [ ] **Step 1: Write failing tests**
  - The migration runs twice cleanly.
  - Ingesting a valid line inserts one row with the share's artifact and version, even when the line carries a different `artifact_id`.
  - The same `ingest_key` gives `duplicate`.
  - Each refusal code is produced by its input and leaves a `denied` event.
  - An annotation on `preview.html` of a report share is ingested; `index.html` on that report share → `file_not_in_version`.
  - The 101st line of one author on one day → `quota_user_day`.
  - A line timed after `revoked_at` → `not_live`, one timed before → inserted.
  - An owner email → `author_role = 'owner'`.
  - `dismiss` of an addressed row → `not_open`.
  - `resolve` with a version older than the annotation's → skipped `version_not_newer`; with a version of another artifact → `bad_version`.
  - `open_for_agents` rows have no `author_email` or `author_identity` key.
  - A hostile body round-trips byte-for-byte.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_annotations_store.py -q` → FAIL.
- [ ] **Step 3: Implement** the migration and the store. Validate `record["anchor"]` and `record["body"]` with Task 1 (`invalid` on error).
- [ ] **Step 4: Run** the new tests plus `test_share_store.py` → PASS.
- [ ] **Step 5: Commit** `feat(annotations): table, store and audit events`.

### Task 4: Worker ingest, viewer projections, YAML settings, owner email on lab shares

**Files:**
- Modify:
  - `herdr_engineering/control_plane/worker.py` (new `AnnotationHousekeeper`, wired next to `ShareHousekeeper`)
  - `herdr_engineering/cli.py` (construct it near line 4007)
  - `herdr_engineering/fleet_config_cli.py` (`SHARE_SLICE_KEYS` += `"annotations"`; validate the mapping)
  - `herdr_engineering/control_plane/artifact_share_api.py` (`share_settings_config` reads `annotations` and `owner_emails`; `_create` appends `owner_emails` to a lab share's `allow_emails`)
  - `herdr_engineering/control_plane/share_bundle.py` (projection writer)
- Test: `tests/control_plane/test_annotation_worker.py`; extend `tests/control_plane/test_artifact_share_api.py` and the fleet-config tests

**Interfaces:**
- Consumes: `JsonlIngestor` (Task 2), `AnnotationStore` (Task 3).
- Produces:
  - `def annotation_settings(raw: dict | None) -> dict`: validated keys and defaults as in Global Constraints. Anything malformed → `FleetConfigError` at load.
  - `def write_own_projection(root: Path, share_id: str, identity: str, rows: list[dict]) -> None` writes `<root>/<share_id>/annotations/<sha256(identity)>.json`. The write is atomic (tmp + `os.replace`), mode 0644, ≤ 1 MiB. It is skipped when `<root>/<share_id>` does not exist and never follows a symlink. `remove_bundle` already removes the projection with the share.
  - `class AnnotationHousekeeper(shares_root, events_dir, marker_dir, settings, *, mirror=None, interval=15.0, clock=time.time)`. `async tick(conn) -> dict | str` runs these steps:
    1. ingest and rotate `annotations.jsonl` (marker `annotation.marker.json` in `marker_dir`)
    2. for `touched_since(last)` rewrite the projection of every author on each touched share
    3. if `mirror` is set, write `annotations.json` for each touched artifact (Task 11 defines the content)
    4. the first tick after start treats every share with annotations as touched

    It never raises out of the worker pass.

- [ ] **Step 1: Write failing tests**
  - A line appended to `annotations.jsonl` becomes a row after one tick, and the author's projection file appears with that row. Another author's projection does not contain it.
  - A dismiss through the store is reflected in the projection on the next tick.
  - A revoked share's directory is not recreated by the projection writer.
  - `annotation_settings` defaults and refusals: non-bool `enabled`, a non-ASCII or invalid email in `owner_emails`, limits ≤ 0.
  - The share slice accepts `annotations`, still refuses unknown keys, and a rendered slice never contains `owner_emails`.
  - A lab share created with `owner_emails: ["owner@lab.test"]` lists it in `allow_emails` and in the create summary, de-duplicated and lower-cased. A public share is unchanged.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new and touched tests → PASS.
- [ ] **Step 5: Commit** `feat(annotations): worker ingest and viewer projections`.

### Task 5: Session v2 (binds the verified email) and the viewer shell route

**Files:**
- Modify:
  - `herdr_engineering/control_plane/share_session.py`
  - `herdr_engineering/control_plane/share_public.py` (`_session` mints v2; new shell route; lab-session `frame-ancestors 'self'`; `VIEWER_CSP` gains `frame-src 'self'`; `_SESSION_SEGMENT` max 768)
  - `herdr_engineering/control_plane/share_static/share.js` (`BASE_PATH` `{1,768}`; after minting, navigate to `/s/<token>/k/<session>/` instead of the entry)
- Test: extend `tests/control_plane/test_share_lab.py` (where `mint_session`/`check_session` are tested today) and `tests/control_plane/test_share_public.py`

**Interfaces:**
- `mint_session(secret, share_id, identity, exp, *, email: str | None = None) -> str`.
  - With `email`: format `<b64 id>.<b64 email>.<exp>.<b64 mac>`. The domain tag is `herdr-share-session-v2` and the MAC covers the length-prefixed email.
  - Without it: today's v1 format, byte-identical.
- `check_session(...) -> {"identity", "exp", "email": str | None}` accepts v1 (email None) and v2.
- `MAX_SESSION = 768`.
- `GET|HEAD /s/<token>/k/<session>/`: the session is checked against the current state (else the uniform 404), then `viewer.html` is served with `VIEWER_CSP` (now with `frame-src 'self'`) and `X-Frame-Options: DENY`.
- Lab-session file responses use `frame-ancestors 'self'`, both in the sandbox CSP (`sandbox_csp(..., frame_ancestors="'self'")`) and in the PDF CSP. Public files keep `'none'`.

- [ ] **Step 1: Write failing tests**
  - v2 round-trip returns the email.
  - Changing one byte of the email part → None.
  - A v1 session still validates with `email is None`.
  - A v2 session for another share → None.
  - Session length ≤ 768 for a 128-character identity and a 254-character email.
  - Shell route: a valid session → 200 viewer HTML; an expired or tampered session, or a revoked share → the uniform 404.
  - A public share has no `k/` route.
  - A lab-session HTML file has `frame-ancestors 'self'` and the same file on a public share still has `'none'`; the PDF likewise.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_share_public.py tests/control_plane/test_share_lab.py -q` → FAIL.
- [ ] **Step 3: Implement.** `_session` passes `grant["email"]` to `mint_session`. `share.js` `openShare` validates the returned `base`, then `location.replace` to the shell path (`base` with the trailing `f/` removed).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(share): email-bound sessions and the lab viewer shell route`.

### Task 6: Annotation routes and HTML selection capture in `share-public`

**Files:** Modify `herdr_engineering/control_plane/share_public.py` (module docstring security rules updated: one body-reading route); Create `herdr_engineering/control_plane/share_static/annotate.js`; Test extend `tests/control_plane/test_share_public.py`

**Interfaces:**
- `ShareConfig` gains `annotations: dict` (from `annotation_settings`) and `annotations_path: Path`, which defaults to `events_path.with_name("annotations.jsonl")`.
- `ShareServer` gains:
  - `annotate_user`, `annotate_share` and `annotate_global` `RateLimiter`s
  - `annotations: EventLog`-like appender `AnnotationLog.append(record: dict) -> bool` (same open/write/cap rules as `EventLog`)
  - `annotate_origin: str | None` (= `public_origin`; a test harness may override it)
- Routes, all under a valid session checked against the current live lab state. Anything else is the uniform 404 plus a `denied` reason.
  - **`GET /s/<token>/k/<session>/annotations`**
    - Returns 200 JSON `{"entry", "entry_type": content_type_for(entry), "can_annotate": bool, "annotations": [own projection rows]}`.
    - `can_annotate` is false when annotations are disabled or the session is v1.
    - Headers: `Cache-Control: no-store`, `nosniff`, no CORS.
  - **`POST /s/<token>/k/<session>/annotations`** is refused unless all of these hold:
    - annotations are enabled
    - the session is v2
    - `Origin` equals `annotate_origin`
    - `X-Herdr-Annotate: 1` is present
    - `Content-Type` is `application/json` (parameters allowed)
    - there is exactly one `Content-Length`, 1–16384, and no `Transfer-Encoding`

    Otherwise the response is the uniform 404 with reason `annotate_refused` (one code: no oracle). Then:
    - The rate limiters are checked (429 `{"error": "rate_limited"}`).
    - The body is read with the socket timeout and passed to `parse_annotation_request` (400 `{"error": <code>}`).
    - The anchor's `file` must be in the state index (400 `bad_file`).
    - One line is appended: `{"v": 1, "annotation_id": "ann_" + token_urlsafe(16), "share_id", "identity", "email", "at", "anchor", "body"}`.
    - 201 `{"annotation": {"annotation_id", "anchor", "body", "status": "submitted", "created_at"}}`.
    - A failed append gives 503 `{"error": "unavailable"}`.
  - **`OPTIONS`** on either annotations path → the uniform 404 (no preflight).
  - **`GET /s/<token>/k/<session>/annotate.js`** → the static `annotate.js` with its `__PARENT_ORIGIN__` placeholder replaced by the JSON-encoded `server.annotate_origin`. The substitution is done on first request and cached per origin value, so a harness that overrides `annotate_origin` after bind gets the right bytes.
- HTML injection happens only for lab-session `GET` of a `text/html` index entry ≤ 2 MiB while annotations are enabled:
  - The whole file is read and verified for size.
  - The response is the file bytes plus `<script src="/s/<token>/k/<session>/annotate.js"></script>` appended.
  - ETag `"<sha256>-a1"`, Content-Length is the new size, Range is ignored (200), If-None-Match is honoured against the derived ETag.
  - HEAD, larger files and public shares use the unchanged `serve_bundle_file`.
- `annotate.js` (content side, ≤ 4 KiB, no dependencies):
  - On `mouseup`/`keyup` (debounced 150 ms) it posts `{type: "herdr-annotate", v: 1, file, exact, prefix, suffix}`.
    - `file` is the path after `/f/`, percent-decoded.
    - `exact` is the selection text clipped to 1000 characters; prefix and suffix are 64 characters of surrounding `document.body.textContent`.
  - It posts `{type: "herdr-annotate", v: 1, file, exact: ""}` on load.
  - It sends to `parent` with the configured target origin. It never reads cookies or storage and never touches the network.

- [ ] **Step 1: Write failing tests** (raw `http.client`, as in the existing file)
  - A valid POST appends exactly one line, returns 201, and the line holds the session's email and identity — never anything taken from the body, even when the body sends `email`.
  - Each refusal writes no line: Origin `null`, Origin missing, Origin of another host, missing custom header, `text/plain`, two `Content-Length` headers, `Transfer-Encoding: chunked`, 16385 bytes, a v1 session, annotations disabled, a public share, an expired session, a revoked share.
  - The 11th POST in a minute by one identity → 429.
  - A file not in the index → 400 `bad_file`.
  - OPTIONS → 404.
  - The GET returns only the caller's projection rows.
  - An injected HTML response ends with the script tag and has the derived ETag and correct Content-Length; a Range request gets 200.
  - A 2 MiB + 1 HTML file is served unmodified.
  - A public share's HTML is never injected.
  - `annotate.js` contains no placeholder and names `server.annotate_origin`, including after the test overrides it.
  - The stdlib request log is still silent: no path reaches a log record.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Extend `_route`'s method check so POST is accepted only for `session` and `k/<session>/annotations`.
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_share_public.py tests/control_plane/test_share_lab.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(share): annotation capture routes`.

### Task 7: Edge nginx admits the annotation POST

**Files:** Modify `deploy/control-plane/collab-edge/share-http.conf`, `share-location.conf`; Test extend `tests/test_share_deploy_config.py` (the `render_edge_conf` tests)

**Interfaces:**
- `location ^~ /s/` is a `^~` prefix match, so nginx never consults a sibling regex location for `/s/...` paths. The new location is therefore **nested inside** `location ^~ /s/`: `location ~ ^/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations$`.
- `if`/`return` and `limit_req` are not inherited into a nested location, so the nested block carries its own copies:
  - its own method guard: GET and POST only, else `return 404`
  - `access_log off`, `error_log /dev/stderr crit`
  - `client_max_body_size 16k`, `proxy_pass_request_body on`
  - `limit_req zone=share_annotate burst=10 nodelay`, with a new zone in `share-http.conf` at 2r/s per address
  - `limit_conn share_conn 16`
  - `proxy_pass http://share`
- `$share_method_ok` admits `"~^POST:/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations\z"`.
- The outer block keeps its `$share_has_body` → 404 rule for every other path.

- [ ] **Step 1: Write failing tests** on the rendered config:
  - The annotations location appears between the opening of `location ^~ /s/ {` and its closing brace (assert nesting by brace depth, not mere presence).
  - The nested block has `proxy_pass_request_body on`, `client_max_body_size 16k` and the `share_annotate` limit.
  - The outer block still has `proxy_pass_request_body off` and the body rule.
  - With sharing disabled the rendered config is still byte-identical to the template.
  - If `nginx` is on PATH, `nginx -t` passes on the rendered config (skip otherwise).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(edge): admit share annotation posts`.

### Task 8: Viewer shell UI (annotate, list own annotations)

**Files:** Modify `herdr_engineering/control_plane/share_static/share.js`, `share.css`, `viewer.html` (a shell region hidden until shell mode); Create `tests/playwright/share-annotations-server.py`, `tests/playwright/share-annotations.test.mjs`

**Interfaces:**
- **Shell mode** runs when `location.pathname` matches `^/s/<token43>/k/<session>/$`:
  - It fetches `annotations` (GET) and renders the header (title "Shared artifact", an expiry note) and the content frame.
    - HTML and other types: `<iframe sandbox="allow-scripts allow-downloads" src="f/<entry>">`.
    - PDF: a frame without the sandbox attribute.
    - Image: an `<img>` of `f/<entry>` with a drag-to-draw region overlay when `entry_type` starts with `image/`.
  - It renders a side panel (stacked below the content at ≤ 640px) with:
    - the current anchor: quote in a `<blockquote>`, page number input for PDF, region box for images, "Whole file" otherwise
    - a `<textarea maxlength=4000>` with a live counter and a Submit button
    - "Your annotations": status Open / Addressed in vN / Dismissed / Submitted
  - Everything is built with `createElement` and `textContent` only.
- **Message intake**: only when `event.source === frame.contentWindow`, `data.type === "herdr-annotate"`, `v === 1`, all fields strings within the Task 1 caps. Anything else is ignored silently.
- **Submit** sends POST JSON with `X-Herdr-Annotate: 1`, `credentials: "omit"`, `cache: "no-store"`.
  - 201 → add the row as "Submitted" and clear the box.
  - 400 → a fixed message per code.
  - 429 → "Too many annotations — wait a minute".
  - 404 → "Your session ended — open the link again", keeping the text in the box.
  - The button is disabled while the request is in flight.
- When `can_annotate` is false the panel shows the list only.
- **Harness** (`share-annotations-server.py`) starts a real `ShareServer` on 127.0.0.1:
  - It builds `LabSettings` directly with a stub authenticator and a fixed secret, sets `server.annotate_origin` to its own `http://127.0.0.1:<port>`, and enables annotations.
  - It seeds three lab shares (an HTML bundle with two pages, a PDF, a PNG) and one public share.
  - It prints `PORT=<n>` and a JSON line of minted v2 shell URLs, plus the public share URL.
  - It exposes `GET /__lines` → the parsed `annotations.jsonl` (test only, on a separate port).

- [ ] **Step 1: Write the Playwright tests**
  - HTML shell: select a sentence inside the frame; the blockquote shows it; submit the hostile comment `<img src=x onerror=window.__x=1>` → the list shows it literally, `window.__x` is undefined in the shell and the frame, and `/__lines` has one line with the quote, prefix and suffix.
  - Navigate to page 2 in the frame; the next annotation's file is `page2.html`.
  - A `fetch` POST from inside the frame (`frame.evaluate`) fails and adds no line.
  - The counter blocks the 4001st character.
  - PDF shell: page 3 plus a quote is submitted as `pdf_page`.
  - Image shell: drawing a box submits `image_region` with values in [0, 1].
  - A frame-busting page (`top.location = "about:blank"`) does not navigate the shell.
  - The public share URL has no annotate UI, and `POST` to a fabricated `k/` path is 404.
  - Revoke mid-session (harness endpoint deletes the state file) → submit shows the session-ended message and keeps the text.
  - Viewport 320px: no horizontal scroll and the panel is below the content.
- [ ] **Step 2: Run** `node --test tests/playwright/share-annotations.test.mjs` → FAIL.
- [ ] **Step 3: Implement** the shell.
- [ ] **Step 4: Run** the new test plus `python3 -m pytest tests/control_plane/test_share_public.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(share): annotate from the lab viewer`.

> Open question for the operator, recorded in this task: the spec says "other types: whole-document comment"; the brief asked for image regions. `image_region` is implemented here as a separable anchor kind. If the operator declines it, drop the overlay and the kind (Task 1 constant), and keep `document` for images.

### Task 9: Owner annotation API and web-client methods

**Files:** Create `herdr_engineering/control_plane/artifact_annotation_api.py`; Modify `herdr_engineering/control_plane/api.py` (call `route_annotations` right after `route_share`), `packages/herdr-web-client/client.js`; Test `tests/control_plane/test_artifact_annotation_api.py`, `packages/herdr-web-client/artifact-annotations.test.js`

**Interfaces:**
- `def route_annotations(handler, server, parts, resource) -> bool`:
  - `GET /api/v1/artifacts/{id}/annotations?status=open|addressed|dismissed|all&limit=1..500` → `{"artifact_id", "annotations": [...]}`, using the `list_for_artifact` fields plus `author_email`, `author_role`, `share_id`.
  - `POST /api/v1/artifacts/{id}/annotations/{aid}/dismiss` with body `{}` → `{"annotation": {...}}`.
  - Both need the `share` permission (non-disclosing 404) and the same cross-origin POST refusal as `route_share`. A body with unknown keys → 400. `not_open` → 409.
  - Any POST answered early closes the connection, as in `route_share`.
- Client: `listArtifactAnnotations(id, { status = "open", signal } = {})`, `dismissArtifactAnnotation(id, annotationId, { signal } = {})`, with ids passed through `encodeURIComponent`.

- [ ] **Step 1: Write failing tests**
  - Without `share` → 404.
  - The list filters by status; limit 0 or 501 → 400.
  - Dismiss on an open annotation → 200 and the next list shows it as dismissed with an `annotation_dismissed` share event.
  - Dismiss of an addressed annotation → 409.
  - A cross-origin POST → 403.
  - Client: exact paths, methods and bodies.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(annotations): owner list and dismiss API`.

### Task 10: Feedback panel in the artifact detail view

**Files:** Create `packages/artifact-feedback-panel.js`, `packages/artifact-feedback-panel.test.js`; Modify `packages/artifacts-view.js` (mount it in `renderArtifactDetail` and `renderReportReader` next to `mountSharePanel`); Modify `packages/artifact-share-panel.js` (`renderEvents` labels the three new kinds)

**Interfaces:**
- Consumes: Task 9 client methods.
- Produces:
  - `export function renderAnchor(anchor)`:
    - `text_quote` → `…prefix<mark>exact</mark>suffix…`, all escaped
    - `pdf_page` → `Page N` plus the quote
    - `image_region` → `Region x%, y% (w% × h%)`
    - `document` → `Whole file`
    - always followed by `in <code>file</code>`
  - `export function renderAnnotationRow(annotation, { artifactId, busy })` returns escaped HTML: author email plus an "owner" badge when `author_role === 'owner'`, version `vN`, anchor, body in `<p style="white-space:pre-wrap;overflow-wrap:anywhere">`, status, a link to `addressed_in_version` built with the version-link format the report reader writes (`artifacts-view.js` ~line 212), and a Dismiss button only when `status === 'open'`.
  - `export function mountFeedbackPanel(container, client, artifactId, { signal }) -> { dispose() }`:
    - status filter Open / Addressed / Dismissed / All (default Open)
    - per-section `<herdr-error>` plus Retry
    - Dismiss asks `confirm()`, disables the button in flight and refreshes the list
  - If plan 3's Share card has a tab bar, mount into a "Feedback" tab panel. Otherwise mount as a `<section data-artifact-feedback>` card after the Share card.

- [ ] **Step 1: Write failing tests**
  - Hostile email, body, quote and file name are escaped.
  - Open rows have Dismiss; addressed rows link to vN and have no Dismiss.
  - The owner badge is shown.
  - A 4000-character unbroken body gets the wrap style.
  - Empty state "No annotations yet. Lab viewers can annotate shared versions; public links cannot."
  - `renderEvents` shows `annotated` as "annotated".
- [ ] **Step 2: Run** `node --test packages/artifact-feedback-panel.test.js` → FAIL.
- [ ] **Step 3: Implement** and mount.
- [ ] **Step 4: Run** `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(web): feedback panel for artifact annotations`.

### Task 11: MCP tools `artifact_annotations` and `artifact_annotation_resolve`

**Files:** Modify `herdr_engineering/control_plane/collab_mcp.py`, `collab_bridge_files.py` (intent kind `annotation_resolve`), `collab_intents.py` (dispatch), `collab_artifacts.py` (`CollabArtifactBridge.annotations` / `.resolve_annotations`), `collab_mirror.py` (writes `artifacts/<id>/annotations.json`), `herdr_engineering/control_plane/worker.py` (`AnnotationHousekeeper` asks the mirror to refresh touched artifacts), `packages/collab-guide.md`, `docs/operations/collaboration-network-guide.md`, `skills/herdr-artifact/SKILL.md` (tool list only); Test `tests/control_plane/test_collab_mcp_annotations.py`, `tests/control_plane/test_annotation_resolve_intent.py`

**Interfaces:**
- Consumes: the "Consumed from plan 3" names, plus `AnnotationStore.open_for_agents` and `AnnotationStore.resolve` (Task 3).
- **Mirror file** `<mirror>/artifacts/<artifact_id>/annotations.json` = `{"artifact_id", "generated_at", "annotations": open_for_agents(...)}`.
  - It holds at most 1 MiB, newest first; when cut, it carries `"truncated": true`.
  - `CollabMirror.publish_annotations(artifact_id, rows)` writes it with `atomic_write_json`, only for artifacts that hold a live grant.
  - The `AnnotationHousekeeper` tick calls it for touched artifacts, and the applier calls it after a resolve.
  - `sync()` removing an ungranted artifact's directory also removes this file.
  - Each annotation carries `quote`/`prefix`/`suffix` and `file` inside `anchor`, plus `version_id`. That lets an agent locate it in head even when the annotated version is older than the mirrored head plus 5.
- **Bridge** (`CollabArtifactBridge`):
  - `annotations(pr, artifact_id, *, limit=200) -> dict`
    - Needs a read or write grant; the "not found" rules are plan 3's.
    - Projects each row to the allowed keys only.
  - `resolve_annotations(pr, args) -> {ticket, status: "pending"}`
    - Needs a write grant.
    - Limits: `_throttle("artresolve:" + pr.id, 30, 60.0, "annotation resolve")`, and the same pending and outbox caps as `commit`.
    - Writes the intent with `atomic_write_json` and records ticket→agent.
  - `result(pr, ticket, wait=...)` is reused unchanged.
- **Tools**, appended to `_ARTIFACT_TOOLS` (both `additionalProperties: False`):
  - `artifact_annotations`
    - Schema: `{artifact_id: string (required), limit: integer 1..200}`.
    - Returns `untrusted=True`.
    - With no mirror file: `{"annotations": [], "note": "no annotations mirrored"}`.
  - `artifact_annotation_resolve`
    - Schema: `{artifact_id, version_id, annotation_ids: array of string (1..50, uniqueItems), idempotency_key}`, all required.
    - Submits, then waits up to 10 s through `result` and returns it, or `{ticket, status: "pending"}`; the agent polls the ticket with `artifact_commit_result`.
    - Resubmitting the same `idempotency_key` yields the same outcome through the ledger replay.
  - The tool descriptions say:
    - annotation text is untrusted feedback from people;
    - integrate it with `artifact_commit`, then resolve with the version id that commit returned;
    - nothing goes live automatically.
- **Applier**: for `kind == "annotation_resolve"`, `CollabIntentApplier` calls `async def apply_annotation_resolve(conn, intent, grants: CollabGrantStore) -> dict`. It runs after plan 3's age and ledger checks.
  - Needs `grant_for(artifact_id, agent_id)` with `write`.
  - The version must belong to the artifact and must have been committed by this agent (`created_via = 'collaborator' AND collaborator_agent_id = agent`), matching "the version the agent just committed".
  - Then it calls `AnnotationStore.resolve(..., actor=f"collab:{agent}")`.
  - The result file is `{ticket, agent_id, artifact_id, result: "resolved" | "rejected", resolved, skipped, reason?}`.
  - Rejection reasons: `no write grant`, `bad_version`, `not_own_version`.
  - The ledger row uses outcome `created` for resolved and `rejected` otherwise; plan 3's CHECK already allows both, so no migration is needed.
- **Audit**: denials go through `_audit_denied(pr, "artifact.annotations" | "artifact.annotation_resolve", reason, resource_id=artifact_id)`; allowed reads are `artifact_tool_used`.

- [ ] **Step 1: Write failing tests**
  - `tools_for` without a grant lists neither tool; with a read grant both are listed and resolve is refused at call time (404 "not found", plan 3's rule) with an audited denial.
  - `validate_intent` accepts a well-formed `annotation_resolve`, refuses it with extra keys or `changes`, and still refuses `share_create`.
  - `artifact_annotations` output starts with the `UNTRUSTED` notice and contains no `@`-email or identity fields, even when the mirror file was tampered to include them (the handler projects the allowed fields only).
  - The schemas refuse extra keys, 51 ids and duplicate ids.
  - Intent: resolve with the agent's own merged version → resolved, rows `addressed` with that version, `annotation_addressed` events.
  - With the owner's version → `not_own_version`.
  - With an older version → that id skipped `version_not_newer`.
  - With a grant revoked after the intent was written → `rejected` with "no write grant".
  - Replaying the same idempotency key → the same result, no second update.
  - The mirror file is rewritten after resolve and no longer lists the resolved ids.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**, and document both tools in `packages/collab-guide.md` and its `docs/operations` mirror: what they return, the untrusted notice, and the commit-then-resolve workflow.
- [ ] **Step 4: Run** the new tests plus `tests/control_plane/test_collab_public.py` and the other tests importing `collab_mcp` (`grep -rln collab_mcp tests`) → PASS.
- [ ] **Step 5: Commit** `feat(collab): annotation tools for granted agents`.

### Task 12: End-to-end owner flow and gates

**Files:** Modify `tests/playwright/artifact-shares-server.py` (in-memory annotation routes with the Task 9 shapes), `tests/playwright/artifact-shares.test.mjs` (or a new `artifact-feedback.test.mjs` wired the same way)

- [ ] **Step 1: Write the Playwright test**
  - The artifact detail page shows the Feedback section with two seeded annotations: one open with a hostile body, one addressed in v3.
  - The hostile body is literal and `window.__x` stays undefined.
  - The addressed row links to v3.
  - Dismiss → the row leaves the Open filter and appears under Dismissed; a double click sends one POST (the stub counts).
  - A server 500 shows the error with Retry, and Retry recovers.
  - 320px viewport: no horizontal scroll.
- [ ] **Step 2: Run** `npm run test:ui` for the file → FAIL, then implement the stub routes until PASS. Fix product code if the test reveals a defect.
- [ ] **Step 3: Run** the UI gates for the touched surfaces (`npm run test:ui-gates`) and update intentionally changed baselines by the repo's procedure. A baseline already red on main is not this plan's to touch.
- [ ] **Step 4: Run** the whole control-plane suite (`python3 -m pytest tests/control_plane -q`) and `npm test` → PASS.
- [ ] **Step 5: Commit** `test(annotations): owner feedback end-to-end`.

---

## Self-review notes

- **Spec coverage (section D):**
  - who may annotate → Tasks 5, 6
  - anonymous/public refused → Tasks 5, 6, 8
  - anchors (HTML text-quote plus file, PDF page plus optional quote, other → whole file; parquet excluded) → Tasks 1, 6, 8
  - capture by injected script plus `postMessage` → Task 6
  - shell button and comment box → Task 8
  - session-authenticated, size-capped, rate-limited POST → Tasks 6, 7
  - plain text never HTML → Tasks 1, 8, 10, 11
  - database-free append plus worker ingest → Tasks 2–4
  - statuses and `addressed_in_version_id` → Task 3
  - size and rotation rules of the events file → Tasks 2, 4
  - owner Feedback tab with Dismiss and version link → Tasks 9, 10, 12
  - annotators see their own annotations and their status → Tasks 4, 6, 8
  - MCP `artifact_annotations` (untrusted) and `artifact_annotation_resolve` → Task 11
  - a new revision never changes a live link → no code changes share state (Global Constraints)
  - owner annotates through the lab link with their email auto-added → Task 4
- **Out of scope:**
  - "share latest" convenience (spec: follow-up)
  - members reading their own feedback via MCP (plan 5; `artifact_annotations` already serves granted agents)
  - highlighting existing annotations inside the content
  - annotator edit or delete
- **Names used across tasks:** `annotation_anchor.parse_annotation_request`, `AnnotationInputError`, `JsonlIngestor`, `AnnotationStore.{ingest, list_for_artifact, dismiss, resolve, own_rows, open_for_agents, touched_since}`, `AnnotationHousekeeper`, `write_own_projection`, `annotation_settings`, `route_annotations`, `listArtifactAnnotations`, `dismissArtifactAnnotation`, `mountFeedbackPanel`, `apply_annotation_resolve`, intent kind `annotation_resolve`, file `annotations.jsonl`, mirror file `annotations.json`. These are consistent across tasks.
- **Operator-only questions:**
  1. Which email(s) the owner signs in with: `owner_emails` is new YAML. Already-live lab shares are immutable, so the owner cannot annotate those unless they are re-shared.
  2. Image region anchors: keep them or drop them (Task 8 note).
  3. Should annotation be on for every lab share, or a per-share opt-in? The plan assumes on for all lab shares, with a global YAML kill switch.
  4. May annotators edit or delete their own annotations? The plan says no in v1.
  5. Are the default caps right: 10/min, 100/day per user, 1000 per share, 4000-character comments?
