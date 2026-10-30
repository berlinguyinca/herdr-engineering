# Artifact annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 4 of the artifact sharing / collaboration design. Signed-in lab viewers and the owner can annotate a lab-shared artifact. Each annotation is bound to the share's pinned version plus an anchor. Annotators can edit or delete their own annotations while they are still open. The owner sees and dismisses annotations in a Feedback section of the Share card. A granted agent reads open annotations and marks them addressed through two MCP tools. Anonymous and public visitors get no annotation surface at all.

**Architecture:**
- **No frames anywhere.** There is no `<iframe>`, `<frame>`, `<object>` or `<embed>`, and no `postMessage` (operator decision 2026-10-30). Each view is a top-level document.
- **Two sibling modes of one lab share, both on the share host** (`control_plane.share.public_url`, for example `https://artifacts.metabolomics.us`):
  - **Live view.** `GET /s/<token>/k/<session>/f/<path>` is today's route and stays byte-for-byte unchanged. The raw bundle file runs top-level with the existing `sandbox allow-scripts allow-downloads …` CSP (`share_public._CSP_TEMPLATE`/`sandbox_csp`), so its scripts run in an opaque origin. It has no annotation layer.
  - **Annotation view** (new). `GET /s/<token>/k/<session>/a/<path>` is a top-level page that `share-public` builds from our own template:
    - It holds a **script-free, sanitized static rendering** of the file. HTML is re-serialized from a parse tree by an allow-list sanitizer in the stdlib. Text types become an escaped `<pre>`. Images become a native `<img>`. PDFs are drawn by a vendored, pinned pdf.js onto canvases with a text layer. Other types get a whole-file comment only.
    - Our `annotate.js` runs as the only script, allowed by a per-response CSP nonce.
    - The page links out to the live view (`../f/<path>`). The live view cannot link back, because that would mean injecting into untrusted content.
- **How hostile content is contained.**
  - Content scripts never run in the annotation view: they are stripped, and in addition `script-src 'nonce-…' 'strict-dynamic'` has no `'self'`.
  - In the live view, content scripts run in an opaque origin. Their requests carry `Origin: null`, and they cannot read the annotation view, which is sent without CORS.
  - So hostile content can neither run next to `annotate.js` nor forge an annotation POST.
  - In the annotation view, the content sits in a **declarative shadow root** (`<template shadowrootmode="open">`). Its CSS cannot restyle our panel, its ids cannot clobber our globals, and `contain: paint` keeps its `position: fixed` boxes inside the content area.
- **Session.**
  - The session stays the HMAC path segment `k/<session>/`. There are no cookies in `share-public`, and the brief's "existing session cookie" does not exist.
  - Annotation writes need a v2 session, which binds the verified email. They also need:
    - the exact share `Origin`
    - `Content-Type: application/json`
    - an `X-Herdr-Annotate: <csrf>` header. The CSRF token is an HMAC of the session, delivered only inside the annotation view, which is not readable cross-origin.
  - The custom header forces a preflight, and `OPTIONS` stays the uniform 404.
- **Storage path.**
  - `share-public` stays database-free. It appends `create` / `edit` / `delete` records to `annotations.jsonl`.
  - The worker ingests them into `artifact_annotations`. It derives artifact and version from the share row and enforces authorship and the `open` state.
  - It writes per-viewer read-back projections onto the shares volume, and the open-annotation list into plan 3's MCP mirror.
- `artifact_annotation_resolve` is a plan-3 intent. The control-plane worker is the authority that applies it.
- **Anchors are against the static render:**
  - `text_quote` for HTML and text
  - `pdf_page` with an optional quote and optional region for PDF
  - `image_region` for images
  - `document` for everything else, or as a fallback

**Tech Stack:**
- Python 3.12 stdlib (`http.server`, `html.parser`) for `share_public.py` and the new `share_render.py`. asyncpg in the control plane and worker.
- Plain ES-module JS in `packages/` and `share_static/`. pdf.js is vendored from `pdfjs-dist@4.10.38` (Apache-2.0) under `share_static/vendor/pdfjs/`.
- Tests: node:test for unit tests, Playwright (Chromium) for UI, pytest with the Postgres fixtures of the existing `test_share_*` / `test_artifact_*` tests.
- Postgres migrations live in `deploy/control-plane/migrations`. The edge nginx lives in `deploy/control-plane/collab-edge`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md`, section D, plus the "Second builder check" bullet "Owner annotations". Section D's "shell / injected script / postMessage" capture is superseded by the operator decisions below; Task 14 updates the spec text. Share-link mechanics: `docs/superpowers/specs/2026-10-30-artifact-share-links-design.md`. Code: `herdr_engineering/control_plane/{share_public,share_session,share_lab,share_bundle,artifact_shares,artifact_share_api,worker}.py`, `share_static/`, `herdr_engineering/fleet_config_cli.py` (`SHARE_KEYS`, `SHARE_SLICE_KEYS`, `share_values`, `render_edge_conf`), `herdr_engineering/cli.py` (`_share_public_server`, `share_settings_config` call sites).

## Decisions (operator, 2026-10-30; binding)

1. **No frames, ever.** No `<iframe>`, `<frame>`, `<object>` or `<embed>` in any viewer HTML or JS this plan writes, and no `postMessage`.
   - The design is the annotation view described above.
   - **Tradeoff, accepted:**
     - Annotations apply to the static render, not to the live app.
     - Interactive content (JS-built charts, apps) cannot be annotated in place. Its viewer leaves a whole-file comment or annotates whatever static text survives sanitizing.
     - Text quotes refer to the sanitized text, so text that a script adds at run time is not quotable.
     - Inline `<svg>`/`<math>`, `<canvas>`, `<video>` and `<audio>` render as a visible "[embedded content omitted]" placeholder.
     - `.md` files are `application/octet-stream` today (`share_bundle._CONTENT_TYPES`), so they get a whole-file comment only. Adding a markdown type is out of scope.
2. **Host.** The share viewer, the live view and the annotation view are served on the share host taken from the new YAML key `control_plane.share.public_url`, for example `https://artifacts.metabolomics.us`. MCP stays on `control_plane.collab.public_url`.
   - **Correction to the brief:** the only existing key is `control_plane.collab.public_url`, which is the MCP host, so it cannot be reused. The new key follows its naming and validation (an https origin with an empty path).
   - When `share.enabled` is true, `share.public_url` is **required** and must differ from the collab host.
   - Links already issued under the MCP host keep working: the MCP host's `/s/` location becomes a `308` to the share host.
   - The operator deploys. The Cognito callback and sign-out URLs, DNS, the certificate and the SNI hostname are deploy steps (Task 14).
3. **Owner emails** come from YAML `control_plane.share.owner_emails` (a list, default `[]`). The API and the worker read it; it is never part of the share slice.
4. **Annotations are on for every lab share.** There is no per-share opt-in. Public shares never get annotations. The global kill switch is `control_plane.share.annotations.enabled`.
5. **Annotators may edit and delete only their own annotations, and only while the status is `open`.** Once `addressed` or `dismissed`, an annotation is frozen.
   - Delete is a soft delete: the status becomes `withdrawn`, and an audit event is kept.
   - The worker is the authority: `share-public` cannot verify authorship durably.
6. **Limits are configurable** under `control_plane.share.annotations`. The defaults are raised for generous capacity:

   | Key | Default | Meaning |
   |---|---|---|
   | `per_user_per_minute` | 60 | creates, edits and deletes per user |
   | `per_share_per_minute` | 600 | per share |
   | `global_per_minute` | 3000 | across all shares |
   | `per_user_per_day` | 2000 | creates per user |
   | `max_per_share` | 20000 | annotations per share |
   | `max_comment_chars` | 4000 | 1–4000; 4000 is also the DB CHECK ceiling |

   The per-share and global caps were re-derived so the per-user cap still binds.
7. **Image and PDF region anchors are kept.** `image_region` is used for images. `pdf_page` takes an optional `region` on that page.
8. **Correction to the brief:** the live view's sandbox CSP already exists (`sandbox allow-scripts allow-downloads; … frame-ancestors 'none'`). It is left unchanged. No route loosens `frame-ancestors`.

## Consumed from plan 3 (`docs/superpowers/plans/2026-10-30-artifact-collaborators-mcp-bridge.md`)

Task 13 builds on these plan-3 names. If plan 3 changes them before it merges, follow plan 3.
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

- **No frames.**
  - No viewer HTML, template or JS written by this plan contains `<iframe`, `<frame`, `<object`, `<embed`, `createElement("iframe")` or `postMessage`.
  - The sanitizer removes those elements from content.
  - Pinned by `tests/control_plane/test_share_no_frames.py` (Task 14).
- **Who may annotate.**
  - Only a lab-share viewer whose **v2** session was minted from verified Cognito tokens that passed `share_lab.authorise`.
  - The owner annotates the same way, through the lab link.
  - Public shares and anonymous visitors have no `a/` route and no annotations route: every such path is the uniform 404.
- **Text is never markup.**
  - Annotation text is plain text everywhere: `textContent` in `annotate.js`, `esc()` in the dashboard, JSON data with the `UNTRUSTED` notice (`untrusted=True`) in MCP.
  - It is never written with `innerHTML`, never interpolated into HTML by the server, and never logged.
- **Anchor kinds.** Validated identically in `share-public` and in the worker by one pure module.
  - `{"kind": "document", "file"}` (any file type)
  - `{"kind": "text_quote", "file", "exact", "prefix", "suffix"}` (HTML and text renders)
  - `{"kind": "pdf_page", "file", "page", "exact"?, "region"?: {"x", "y", "w", "h"}}` (PDF)
  - `{"kind": "image_region", "file", "x", "y", "w", "h"}` (images, including SVG shown through `<img>`)
  - Parquet data views are not annotatable.
- **Caps.** One constant each in `annotation_anchor.py`:
  - request body ≤ 32 KiB (4000 astral characters plus the anchor exceed the old 16 KiB)
  - comment 1–`max_comment_chars` characters after strip (the hard ceiling `MAX_COMMENT_CHARS = 4000`); no C0 controls except `\n` and `\t`, no NUL, no surrogates
  - `exact` 1–1000 characters
  - `prefix` and `suffix` ≤ 64 characters each
  - `file` normalised by `share_bundle.normalise_name` and ≤ 1024 bytes
  - `page` 1–100000
  - region values are finite floats in [0, 1] with `x + w ≤ 1` and `y + h ≤ 1`
- **Rate limits** (Decision 6).
  - `share-public` (in-memory `RateLimiter`): per (share, identity) `per_user_per_minute`; per share `per_share_per_minute`; global `global_per_minute`. Creates, edits and deletes all count.
  - Durable caps, enforced at worker ingest and surviving `share-public` restarts: creates per (share, author) per day `per_user_per_day`; per share `max_per_share` (withdrawn rows count).
  - The edge allows 5 r/s per address with a burst of 30 on the annotations API.
  - Every refusal is a `denied` share event with a fixed reason code.
- **Annotations file.** `<events_dir>/annotations.jsonl` follows the same rules as `events.jsonl` with its own constants:
  - one `os.write` per line under `O_APPEND`
  - line ≤ 64 KiB (`ensure_ascii` JSON of 4000 astral characters is about 48 KiB)
  - file cap `ANNOTATIONS_MAX_BYTES = 32 MiB`; when it is full the append is refused with 503 and the viewer keeps its text
  - rotation past 4 MiB
  - idempotent ingest keyed by `sha256(line):offset`
- **Version pinning.**
  - An annotation stores `artifact_id` and `version_id`, both taken from the `artifact_shares` row at ingest, plus its anchor.
  - A new revision never changes a live share link. Addressing an annotation only records `addressed_in_version_id`.
- **Annotation-view response headers.**
  - `Content-Security-Policy: default-src 'none'; script-src 'nonce-<n>' 'strict-dynamic'; style-src-elem <origin>/s/static/ <origin>/s/<token>/ 'unsafe-inline'; style-src-attr 'unsafe-inline'; img-src <origin>/s/<token>/ data: blob:; font-src <origin>/s/<token>/ <origin>/s/static/ data:; connect-src <origin>/s/<token>/; worker-src <origin>/s/static/vendor/pdfjs/; frame-src 'none'; child-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
    - Here `<origin>` is the share origin and `<n>` is `secrets.token_urlsafe(18)`, fresh per response.
    - There is no `'self'` in `script-src`: bundle `.js` files are same-origin.
    - Content styles are allowed (`unsafe-inline` for CSS only). The fetch directives confine CSS exfiltration to this share's own prefix.
  - Other headers: `X-Frame-Options: DENY`, `Cache-Control: no-store`, `nosniff`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`.
  - **No CORS headers.** The annotation view, the annotations JSON and every annotations POST response are sent with `cors=False`. `serve_bundle_file` defaults to `cors=True` (`ACAO: *`), and the live view's scripts hold the session path, so these responses must not be readable cross-origin.
- **Configuration is YAML only** (no environment variables):
  - `control_plane.share.public_url`: an https origin with an empty path. Required when `share.enabled`. It must not equal `control_plane.collab.public_url`. Added to `SHARE_KEYS` and `SHARE_SLICE_KEYS`.
  - `control_plane.share.annotations`: `enabled` (bool, default `true`) plus the Decision 6 limits. Added to `SHARE_KEYS` and `SHARE_SLICE_KEYS`.
  - `control_plane.share.owner_emails`: a list, default `[]`. Added to `SHARE_KEYS` only, NOT `SHARE_SLICE_KEYS`, so the operator's email never reaches `share-public`.
  - The edge certificate directory follows the existing compose pattern: `control_plane.compose.HERDR_SHARE_CERT_DIR`.
- **Migration.** Number `0045`. 0043 is taken by `feat/artifact-collab-gui`, 0044 is plan 3's and 0046 is plan 5's. Re-check `origin/main` and open branches before committing and take the next free number. The migration must be additive and idempotent, with a `DOWN:` comment like 0043.
- **Never logged or stored outside the response that needs it:** tokens, sessions, CSRF tokens, nonces, `Authorization` and `X-Id-Token` values. Author emails reach only the owner API and the author's own projection. They never reach the MCP mirror or an agent.
- **Code style.** Python: `from __future__ import annotations`, type hints, the surrounding module's style. JS: ES modules (classic script for `share.js`, as today), 2-space indent, the conventions of `artifact-share-panel.js` (`esc()`, `hd-*` classes, `data-*` test hooks, no inline handlers).
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Hostile bundle content cannot annotate as the viewer.**
  - In the live view, a content script that knows the session path and tries a `fetch` POST to `annotations` gets the 404, with no line written. That holds whether it sends no custom header (no CSRF), `Origin: null`, or triggers an `OPTIONS` preflight.
  - Its attempt to `fetch` the annotation view and read the CSRF meta fails, because the response carries no CORS headers.
  - In the annotation view, a `<script>`, `onerror=`, `javascript:` link, `<meta http-equiv=refresh>`, `<base>`, `<form>`, `<svg><script>`, `<math>` or `<noscript>` mXSS payload in the content never runs.
  - Pinned in Tasks 7, 8 and 10.
- **Hostile annotation text** (`<img src=x onerror=…>`, `"><script>`, RTL override `‮`, zero-width characters, 4000 characters of one unbroken word) appears literally and wraps. It never executes in the annotation panel, the Feedback panel or the event drawer, and in MCP it arrives inside the `UNTRUSTED` wrapper. Pinned in Tasks 10, 12, 13 and 14.
- **A session or share that dies mid-use.**
  - Revoke, expiry or session expiry between page load and submit or edit → the POST gets the uniform 404, and the panel shows "Your session ended — open the link again" with the typed comment kept in the box.
  - A line written before the revoke is still ingested when its `at` precedes `revoked_at`; one after is refused.
  - Pinned in Tasks 4, 8 and 10.
- **Forged or replayed lines from a compromised or restarted `share-public`:**
  - unknown share, public share, file not in the pinned version, quota exceeded, duplicate `annotation_id`, an `artifact_id` or `version_id` field in the line (ignored)
  - an edit or delete by another identity, or of a non-open row
  - Each is either refused with a `denied` event or applied exactly once.
  - Pinned in Tasks 3 and 4.
- **Content that defeats the static render.**
  - An HTML entry over the render cap, a page whose CSS sets `* { display: none }` or `position: fixed` overlays, or a page with only script-built content still shows our panel. A whole-file comment is always possible.
  - A malformed PDF shows "This PDF could not be drawn" and still allows a whole-file comment.
  - Pinned in Tasks 7 and 10.

---

### Task 1: Anchor and comment validation (pure)

**Files:** Create `herdr_engineering/control_plane/annotation_anchor.py`; Test `tests/control_plane/test_annotation_anchor.py`

**Interfaces:**
- Produces:
  - Constants: `MAX_BODY_BYTES = 32768`, `MAX_COMMENT_CHARS = 4000`, `MAX_EXACT_CHARS = 1000`, `MAX_CONTEXT_CHARS = 64`, `MAX_PAGE = 100000`, `ANCHOR_KINDS = ("document", "text_quote", "pdf_page", "image_region")`, `ANNOTATION_ID_RE = re.compile(r"ann_[A-Za-z0-9_-]{22}")`.
  - `class AnnotationInputError(ValueError)` with `.code` in `bad_anchor | bad_file | empty | too_long | bad_chars | bad_id`.
  - `def clean_comment(value: object, *, max_chars: int = MAX_COMMENT_CHARS) -> str`
  - `def clean_anchor(value: object) -> dict` returns a new dict holding only the allowed keys for its kind, with `file` normalised. A `pdf_page` `region` is validated like `image_region`'s fields.
  - `def parse_annotation_request(raw: bytes, *, max_chars: int = MAX_COMMENT_CHARS) -> tuple[dict, str]` parses `{"anchor": {...}, "comment": "..."}`. Unknown top-level keys → `bad_anchor`.
  - `def parse_edit_request(raw: bytes, *, max_chars: int = MAX_COMMENT_CHARS) -> str` parses `{"comment"}` only.
  - `def parse_delete_request(raw: bytes) -> None` accepts only `{}`.
- Never raises anything but `AnnotationInputError`.

- [ ] **Step 1: Write failing tests**
  - Each kind accepted with its minimal and maximal fields, including `pdf_page` with and without `region`.
  - Extra keys stripped inside the anchor but refused at top level.
  - Boundaries exact:
    - comment 4000 accepted, 4001 → `too_long`; with `max_chars=500`, 501 → `too_long`
    - `exact` 1000 / 1001
    - prefix 64 / 65
    - page 0, 1, 100000 and 100001
    - `True` as page refused
    - region (both kinds) with `x + w = 1.0000001` refused; NaN and inf refused
  - Comment `"  \n "` → `empty`. `"\x00"`, `"\x1b"` and `"\ud800"` → `bad_chars`. `"\n"` and `"\t"` kept.
  - `file` values `"../a"`, `"a//b"`, `"/abs"` and a 1025-byte name → `bad_file`.
  - Non-JSON, a JSON list, 32769 bytes → `bad_anchor` / `too_long`.
  - A body of 4000 `"😀"` plus a 1000-character `exact` (about 21 KiB as UTF-8 JSON) is accepted.
  - `parse_edit_request(b'{"comment":"x","anchor":{}}')` → `bad_anchor`; `parse_delete_request(b'{"x":1}')` → `bad_anchor`.
  - A hostile comment `<img src=x onerror=1>` is returned unchanged (stored as text, not filtered).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_annotation_anchor.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement.** Reuse `share_bundle.normalise_name` for `file`. Parse with `json.loads` on the bytes after a length check.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(annotations): anchor and comment validation`.

### Task 2: Reusable JSONL ingestor (refactor, behaviour pinned)

**Files:** Create `herdr_engineering/control_plane/jsonl_ingest.py`; Modify `herdr_engineering/control_plane/artifact_shares.py` (`ShareStore.ingest_events_file` / `ingest_and_rotate_events` delegate); Test: existing `tests/control_plane/test_share_store.py`, `test_share_worker.py`, plus new `tests/control_plane/test_jsonl_ingest.py`

**Interfaces:**
- Produces:
  - `class JsonlIngestor` with `__init__(self, conn, *, parse_line: Callable[[bytes], Any | None], store: Callable[[Any, Any, str], Awaitable[bool]], max_line_bytes: int = 16384)`. Here `store(conn, parsed, ingest_key)` runs inside a per-line savepoint and returns True when a row was inserted. Annotations pass `max_line_bytes=65536`.
  - `async def ingest_file(self, path: Path, marker: Path) -> int`
  - `async def ingest_and_rotate(self, path: Path, marker: Path, *, rotate_bytes: int, now: float, grace: float) -> int`
- The offset/fingerprint marker, the torn-tail rule, oversize-line skipping, the rotation to `.1`, the regular-file-only open and the poison-line skip move here unchanged from `ShareStore`.

- [ ] **Step 1: Write failing tests** in `test_jsonl_ingest.py` with an in-memory `store` callback (no Postgres):
  - A torn last line waits for its newline.
  - A replay after deleting the marker calls `store` with the same `ingest_key` values.
  - A symlink planted at `path` is ignored.
  - A `store` raising `ValueError` skips and consumes that line.
  - A 40 KiB line is skipped at the default limit and passed with `max_line_bytes=65536`.
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
    - `status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','addressed','dismissed','withdrawn'))`
    - `addressed_in_version_id text REFERENCES artifact_versions`
    - `resolved_by text`
    - `resolved_at timestamptz`
    - `edited_at timestamptz`
    - `withdrawn_at timestamptz`
    - `created_at timestamptz NOT NULL`
    - `ingested_at timestamptz NOT NULL DEFAULT now()`
    - `updated_at timestamptz NOT NULL DEFAULT now()`
    - `ingest_key text UNIQUE`
  - Table CHECK: `(status = 'addressed') = (addressed_in_version_id IS NOT NULL)`.
  - Indexes `(artifact_id, status, created_at DESC, annotation_id)`, `(share_id, author_identity, created_at DESC)`, `(updated_at)`.
  - Table `artifact_annotation_ops(ingest_key text PRIMARY KEY, annotation_id text NOT NULL, op text NOT NULL CHECK (op IN ('edit','delete')), at timestamptz NOT NULL)`. This is the idempotency ledger for edit and delete lines.
  - Widen the `artifact_share_events.kind` CHECK to add `'annotated'`, `'annotation_edited'`, `'annotation_withdrawn'`, `'annotation_dismissed'`, `'annotation_addressed'`. Drop and re-add the constraint by name inside a `DO` block that is a no-op when already widened.
- **Store** `AnnotationStore(conn)`:
  - `async def ingest(self, conn, record: dict, ingest_key: str, *, owner_emails: frozenset[str], per_user_per_day: int, max_per_share: int, max_comment_chars: int) -> str` dispatches on `record.get("op", "create")`.
    - **`create`** returns `"inserted"`, `"duplicate"`, or a refusal code: `unknown_share`, `not_lab`, `not_live`, `file_not_in_version`, `quota_user_day`, `quota_share`, `invalid`.
      - `artifact_id` and `version_id` come from the share row. Fields of that name in `record` are ignored.
      - `file` must be one of the names the share actually exposes, from `shared_file_names(artifact_row, version_row) -> frozenset[str]`. This is a new helper in `artifact_share_api.py`, factored out of `_plan` and used by it: `{PREVIEW_NAME}` for a report artifact, otherwise the normalised `output_manifest` names.
      - The share must have been live at `record["at"]` (`approved_at ≤ at < revoked_at`, or `revoked_at` NULL), and `at` is clamped to ≤ now + 5 min.
      - `author_role` is `owner` when the email is in `owner_emails`.
    - **`edit` / `delete`** return `"edited"` / `"withdrawn"`, `"duplicate"` (the `ingest_key` is already in `artifact_annotation_ops`), or a refusal: `not_found`, `not_author` (identity or share differs from the row), `not_open`, `not_live`, `invalid`.
      - An edit sets `body`, `edited_at` and `updated_at`.
      - A delete sets `status='withdrawn'`, `withdrawn_at` and `updated_at`.
    - A refusal writes a `denied` share event with `{"reason": "annotation_" + code}`. Success writes `annotated` / `annotation_edited` / `annotation_withdrawn` with `{"annotation_id", "file"}`.
  - `async def list_for_artifact(self, artifact_id: str, *, status: str = "open", limit: int = 200, before: str | None = None) -> tuple[list[dict], str | None]`.
    - Rows carry `version_number`, `addressed_in_version_number`, `edited_at`.
    - `status` takes `open | addressed | dismissed | withdrawn | all`; `limit` is 1–500.
    - `before` and the returned next cursor are the opaque `"<created_at iso>|<annotation_id>"`.
  - `async def dismiss(self, artifact_id: str, annotation_id: str, *, actor: str) -> dict`. Raises `AnnotationError(code="not_found" | "not_open")`. Dismissing an already dismissed annotation returns the row unchanged.
  - `async def resolve(self, artifact_id: str, annotation_ids: list[str], *, version_id: str, actor: str) -> dict` → `{"resolved": [ids], "skipped": [{"annotation_id", "reason"}]}`.
    - Skip reasons: `not_found`, `not_open` (this includes `withdrawn`), `version_not_newer`.
    - Refuses a `version_id` that is not of this artifact with `AnnotationError("bad_version")`.
    - Writes one `annotation_addressed` event per resolved row.
  - `async def own_rows(self, share_id: str) -> dict[str, list[dict]]` → author_identity → newest 500 rows, withdrawn excluded. Fields: `annotation_id, file, anchor, body, status, version_number, addressed_in_version_number, created_at, edited_at`.
  - `async def open_for_agents(self, artifact_id: str, limit: int = 200) -> list[dict]`. Fields: `annotation_id, version_id, version_number, file, anchor, body, author_role, created_at, edited_at`. No email and no identity.
  - `async def touched_since(self, since: datetime) -> tuple[set[str], set[str]]` → (share ids, artifact ids) of rows with `updated_at > since`.

- [ ] **Step 1: Write failing tests**
  - The migration runs twice cleanly.
  - Ingesting a valid create inserts one row with the share's artifact and version, even when the line carries a different `artifact_id`.
  - The same `ingest_key` gives `duplicate`.
  - Each create refusal code is produced by its input and leaves a `denied` event.
  - An annotation on `preview.html` of a report share is ingested; `index.html` on that report share → `file_not_in_version`.
  - The 2001st create of one author on one day → `quota_user_day`; with `max_per_share=3`, the 4th create on a share → `quota_share`.
  - A line timed after `revoked_at` → `not_live`, one timed before → inserted.
  - An owner email → `author_role = 'owner'`.
  - Edit by the author of an open row → `edited`, new body and `edited_at`, one `annotation_edited` event.
  - Edit by another identity → `not_author`.
  - Edit of a dismissed row → `not_open`.
  - Replaying the edit line → `duplicate`, no second event.
  - Delete by the author → `withdrawn`. The row leaves `own_rows`, `open_for_agents` and the `open` list, and stays under `withdrawn`.
  - `dismiss` of an addressed row → `not_open`.
  - `resolve` with a version older than the annotation's → skipped `version_not_newer`; with a version of another artifact → `bad_version`; on a withdrawn row → skipped `not_open`.
  - `list_for_artifact` paging: 3 rows with `limit=2` → 2 rows plus a cursor; the next call returns the third and cursor `None`.
  - `open_for_agents` rows have no `author_email` or `author_identity` key.
  - A hostile body round-trips byte-for-byte.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_annotations_store.py -q` → FAIL.
- [ ] **Step 3: Implement** the migration and the store. Validate `record["anchor"]` and `record["body"]` with Task 1 (`invalid` on error).
- [ ] **Step 4: Run** the new tests plus `test_share_store.py` → PASS.
- [ ] **Step 5: Commit** `feat(annotations): table, store, edit/withdraw and audit events`.

### Task 4: Worker ingest, viewer projections, YAML settings, owner email on lab shares

**Files:**
- Modify:
  - `herdr_engineering/control_plane/worker.py` (new `AnnotationHousekeeper`, wired next to `ShareHousekeeper`)
  - `herdr_engineering/cli.py` (construct it near line 4007)
  - `herdr_engineering/fleet_config_cli.py` (`SHARE_KEYS` += `"annotations"`, `"owner_emails"`; `SHARE_SLICE_KEYS` += `"annotations"`; validate both in `share_values`)
  - `herdr_engineering/control_plane/artifact_share_api.py` (`share_settings_config` reads `annotations` and `owner_emails`; `_create` appends `owner_emails` to a lab share's `allow_emails`)
  - `herdr_engineering/control_plane/share_bundle.py` (projection writer)
- Test: `tests/control_plane/test_annotation_worker.py`; extend `tests/control_plane/test_artifact_share_api.py` and the fleet-config tests

**Interfaces:**
- Consumes: `JsonlIngestor` (Task 2), `AnnotationStore` (Task 3).
- Produces:
  - `ANNOTATION_DEFAULTS = {"enabled": True, "per_user_per_minute": 60, "per_share_per_minute": 600, "global_per_minute": 3000, "per_user_per_day": 2000, "max_per_share": 20000, "max_comment_chars": 4000}` in `annotation_anchor.py`.
  - `def annotation_settings(raw: dict | None) -> dict` merges `raw` over the defaults. Refused at load with `FleetConfigError`: unknown keys, a non-bool `enabled`, a non-int or ≤ 0 limit, and `max_comment_chars` outside 1–4000.
  - `def write_own_projection(root: Path, share_id: str, identity: str, rows: list[dict]) -> None` writes `<root>/<share_id>/annotations/<sha256(identity)>.json`. The write is atomic (tmp + `os.replace`), mode 0644, ≤ 2 MiB (newest-first, cut with `"truncated": true`). It is skipped when `<root>/<share_id>` does not exist and never follows a symlink. `remove_bundle` already removes the projection with the share.
  - `class AnnotationHousekeeper(shares_root, events_dir, marker_dir, settings, *, owner_emails, mirror=None, interval=15.0, clock=time.time)`. `async tick(conn) -> dict | str` runs these steps:
    1. ingest and rotate `annotations.jsonl` (marker `annotation.marker.json` in `marker_dir`, `max_line_bytes=65536`)
    2. for `touched_since(last)` rewrite the projection of every author on each touched share (withdrawn rows drop out)
    3. if `mirror` is set, write `annotations.json` for each touched artifact (Task 13 defines the content)
    4. the first tick after start treats every share with annotations as touched

    It never raises out of the worker pass.

- [ ] **Step 1: Write failing tests**
  - A create line appended to `annotations.jsonl` becomes a row after one tick, and the author's projection file appears with that row. Another author's projection does not contain it.
  - An edit line by the author is reflected in the projection on the next tick; a delete line removes the row from it.
  - A dismiss through the store is reflected in the projection on the next tick.
  - A revoked share's directory is not recreated by the projection writer.
  - `annotation_settings`: the defaults equal `ANNOTATION_DEFAULTS`; refusals for a non-bool `enabled`, `per_user_per_minute: 0`, `max_comment_chars: 4001`, and an unknown key.
  - `owner_emails` with a non-ASCII or invalid email → `FleetConfigError`.
  - `share_values` accepts `annotations` and `owner_emails`, still refuses unknown keys, and a rendered slice contains `annotations` but never `owner_emails`.
  - A lab share created with `owner_emails: ["owner@lab.test"]` lists it in `allow_emails` and in the create summary, de-duplicated and lower-cased. A public share is unchanged.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new and touched tests → PASS.
- [ ] **Step 5: Commit** `feat(annotations): worker ingest and viewer projections`.

### Task 5: Share host from YAML (`control_plane.share.public_url`)

**Files:** Modify `herdr_engineering/fleet_config_cli.py` (`SHARE_KEYS`, `SHARE_SLICE_KEYS` += `"public_url"`; `share_values` validates it), `herdr_engineering/cli.py` (`share_settings_config(share, public_url=share["public_url"])` at the ~3154 call site and every other call site; `_share_public_server` passes `share.get("public_url")` as `ShareConfig.public_url`; `_share_lab_problems` names `control_plane.share.public_url`), `herdr_engineering/control_plane/artifact_share_api.py` (error text names the new key); Test extend the fleet-config tests, `tests/control_plane/test_artifact_share_api.py`, `tests/control_plane/test_share_lab.py`

**Interfaces:**
- `share_values(cfg)` with `share.enabled: true` checks that `control_plane.share.public_url` is an https origin with path `""` or `/`, no query or fragment, and a netloc different from `control_plane.collab.public_url`'s. Otherwise it raises `FleetConfigError` naming the key: "share links and the lab sign-in callback /s/callback live at the root of the share host; MCP stays on control_plane.collab.public_url".
  - The rule that `control_plane.collab.public_url` is an https origin still holds, because `collab-public` needs it.
- `ShareConfig.public_url` is now the share host. Through it come `lab_settings` (redirect_uri `<share>/s/callback`), `ShareServer.public_origin` (sandbox CSP and annotation-view CSP) and the link base in `artifact_share_api` (`settings["public_url"]` at ~line 266).
- `SHARE_COLLAB_KEYS` is unchanged.

- [ ] **Step 1: Write failing tests**
  - Enabled share without `share.public_url` → `FleetConfigError` naming `control_plane.share.public_url`.
  - `share.public_url` equal to the collab URL → refused.
  - A path, query or `http://` → refused.
  - A valid config renders a slice whose `share.public_url` is the share host.
  - An approved share's URL starts with `https://artifacts.example.test/s/`.
  - `lab_settings(...).config_json` has `redirect_uri` `https://artifacts.example.test/s/callback`.
  - `sandbox_csp` names the share origin.
  - Disabled share: nothing new is required.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_artifact_share_api.py tests/control_plane/test_share_lab.py tests/test_share_deploy_config.py -q` plus the fleet-config tests → PASS.
- [ ] **Step 5: Commit** `feat(share): serve share links from their own host (control_plane.share.public_url)`.

### Task 6: Session v2 (binds the verified email), CSRF token, and the `#annotate` landing

**Files:**
- Modify:
  - `herdr_engineering/control_plane/share_session.py`
  - `herdr_engineering/control_plane/share_public.py` (`_session` mints v2; `_SESSION_SEGMENT` max 768)
  - `herdr_engineering/control_plane/share_static/share.js` (`BASE_PATH` `{1,768}`; the `#annotate` mode)
- Test: extend `tests/control_plane/test_share_lab.py` (where `mint_session`/`check_session` are tested today) and `tests/control_plane/test_share_public.py`

**Interfaces:**
- `mint_session(secret, share_id, identity, exp, *, email: str | None = None) -> str`.
  - With `email`: format `<b64 id>.<b64 email>.<exp>.<b64 mac>`. The domain tag is `herdr-share-session-v2` and the MAC covers the length-prefixed email.
  - Without it: today's v1 format, byte-identical.
- `check_session(...) -> {"identity", "exp", "email": str | None}` accepts v1 (email None) and v2.
- `MAX_SESSION = 768`.
- `def csrf_token(secret: bytes, share_id: str, session: str) -> str` = b64url(HMAC-SHA256(secret, b"herdr-annotate-csrf-v1\x00" + len-prefixed share_id + len-prefixed session)), 43 characters. `def check_csrf(secret, share_id, session, value) -> bool` uses `hmac.compare_digest`.
- The session response is unchanged in shape (`{"base": ".../k/<session>/f/", "entry", "expires_at"}`).
- `share.js`:
  - On `/s/<token>/` it records `mode: location.hash === "#annotate" ? "annotate" : "view"` in the pending sessionStorage entry, next to `share`.
  - After minting, `view` replaces to `base + entry` exactly as today. `annotate` replaces to `base` with its trailing `f/` swapped for `a/`, plus the entry.
  - No other behaviour changes.

- [ ] **Step 1: Write failing tests**
  - v2 round-trip returns the email.
  - Changing one byte of the email part → None.
  - A v1 session still validates with `email is None`.
  - A v2 session for another share → None.
  - Session length ≤ 768 for a 128-character identity and a 254-character email.
  - `csrf_token` is stable for one session, differs across sessions and shares, and `check_csrf` refuses a one-character change.
  - `_session` now returns a v2 session (the email is present after `check_session`).
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_share_public.py tests/control_plane/test_share_lab.py -q` → FAIL.
- [ ] **Step 3: Implement.** `_session` passes `grant["email"]` to `mint_session`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(share): email-bound sessions, annotate CSRF token, #annotate landing`.

### Task 7: Static render and sanitizer (pure)

**Files:** Create `herdr_engineering/control_plane/share_render.py`; Test `tests/control_plane/test_share_render.py`

**Interfaces:**
- Constants:
  - `RENDER_VERSION = 1`
  - `MAX_RENDER_INPUT = 2 * 1024 * 1024`
  - `MAX_RENDER_OUTPUT = 4 * 1024 * 1024`
  - `MAX_ELEMENTS = 100_000`
  - `MAX_DEPTH = 256`: deeper elements are flattened, keeping their children's text
  - `OMITTED = '<span class="herdr-omitted">[embedded content omitted]</span>'`
- `def render_kind(name: str) -> str`, based on `content_type_for`:
  - `"html"` for `text/html`
  - `"text"` for other `text/*` and `application/json`
  - `"image"` for `image/*`
  - `"pdf"` for `application/pdf`
  - `"document"` otherwise, including parquet, wasm, woff2 and octet-stream
- `def sanitize_html(data: bytes, *, name: str, index: Collection[str]) -> str` returns a well-formed HTML fragment. `name` is the file's normalised bundle path and `index` holds the share's file names. The fragment is built by an `html.parser.HTMLParser` subclass (`convert_charrefs=True`) that re-serializes from its own token stream. Raw input is never copied into the output.
  - **Text** is `html.escape(..., quote=False)`. **Attribute values** are `html.escape(..., quote=True)` and always double-quoted.
  - **Kept elements** (tag kept, attributes filtered):
    - `a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup data dd del details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main mark nav ol p pre q rp rt ruby s samp section small span strong sub summary sup table tbody td tfoot th thead time tr u ul var wbr`
    - `style`: its text is kept with every `</` written as `<\/`.
    - `link`: only `rel="stylesheet"` with a resolvable `href`.
  - **Dropped with their content:**
    - `script`, `template`, `iframe`, `frame`, `frameset`, `object`, `embed`, `applet`, `portal`, `fencedframe`, `svg`, `math`, `canvas`, `video`, `audio`, `textarea`, `select`, `title`, `head`'s `meta` and `base`.
    - The frame, plugin, `svg`, `math`, `canvas`, `video` and `audio` elements each leave `OMITTED` in their place.
  - **Tag dropped, children kept:** `noscript`, `form`, `button`, `label`, `fieldset`, `legend`, `html`, `head`, `body`, and every unknown element.
  - **Attributes** (everything else is dropped, including every `on*`, `nonce`, `srcset`, `name`, `formaction`, `xlink:href` and `http-equiv`):
    - Global: `class`, `id`, `title`, `lang`, `dir`, `role`, `aria-*`, `style`.
    - Per element:
      - `a`: `href`
      - `img`: `src alt width height`
      - `td`/`th`: `colspan rowspan headers scope`
      - `ol`: `start reversed type`
      - `li`: `value`
      - `col`/`colgroup`: `span`
      - `time`/`data`: `datetime value`
      - `link`: `rel href`
  - **URLs** (`href` and `src`):
    - A relative URL is resolved against `name`'s directory and normalised with `normalise_name`. If it is in `index`, it is rewritten relative to the current page: `a` → `<"../" × depth>a/<path>` (the annotation view of that file); `img` and `link` → `<"../" × depth>f/<path>`. A fragment is kept.
    - A fragment-only `#x` is kept.
    - For `a`, an absolute `http:`/`https:`/`mailto:` URL is kept and gets `rel="noopener noreferrer" target="_blank"` added.
    - For `img`, a `data:image/(png|jpeg|gif|webp);base64,` URL is kept.
    - Anything else (`javascript:`, `vbscript:`, other `data:`, `//host`, a name outside `index`) drops the attribute.
  - **CSS `url(...)`** in `<style>` text and `style` attributes: a relative URL naming an index file is rewritten to the `f/` path by one bounded, non-backtracking regex. Other URLs are left alone; the CSP blocks them.
  - **Caps:** input over `MAX_RENDER_INPUT` or more than `MAX_ELEMENTS` elements → raises `RenderTooLarge`. Output over `MAX_RENDER_OUTPUT` → `RenderTooLarge`. Undecodable bytes decode with `errors="replace"`.
- `def render_text(data: bytes) -> str` → `<pre class="herdr-text">…escaped…</pre>`, capped like HTML.
- `def wrap_content(kind: str, inner: str) -> str` → `<div id="herdr-content" data-kind="<kind>"><template shadowrootmode="open">` + inner + `</template></div>` for `html` and `text`. Images, PDFs and documents get no content fragment: `annotate.js` builds those.
- `class RenderCache(max_entries=32, max_bytes=64 * 1024 * 1024)`, thread-safe LRU keyed by `(sha256, name, RENDER_VERSION)`. The output holds only relative URLs and no session, token or nonce, so it is safe to share across viewers.

- [ ] **Step 1: Write failing tests** (one assertion group per line)
  - `<script>alert(1)</script>`, `<img src=x onerror=alert(1)>`, `<a href="javascript:alert(1)">`, `<a href=" JaVaScRiPt:alert(1)">`, `<meta http-equiv=refresh content="0;url=//evil">`, `<base href=//evil/>`, `<form action=//evil><input>`, `<svg><script>…</script></svg>`, `<math><mtext><table><mglyph><style><img src=x onerror=1>`, `<noscript><p title="</noscript><img src=x onerror=1>">` and `<style></style><img src=x onerror=1>`.
    - For each: the output parsed again by `html.parser` contains no `script` element, no attribute starting `on`, no `javascript:` and no `<base`/`<meta`.
  - `<iframe src=a.html>`, `<frame>`, `<object data=x>`, `<embed src=x>` → the output contains none of `<iframe`, `<frame`, `<object`, `<embed` (case-insensitive) and contains `OMITTED`.
  - Unterminated markup (`<p title="abc`, `<div><span>`) at the end of the input gives balanced output, and text after the fragment is unaffected (`wrap_content` output ends with `</template></div>`).
  - Links:
    - `href="page2.html"` from `index.html` → `a/page2.html`.
    - From `sub/x.html`, `href="../index.html"` → `../a/index.html`.
    - `<img src="img/p.png">` → `f/img/p.png`.
    - A name not in the index → no `href`.
    - `https://example.org` gets `rel="noopener noreferrer"`.
    - A `data:image/svg+xml` src is dropped.
  - `style="color:red"` is kept; `<style>a{}</style>` is kept; `</style>` inside CSS text cannot close the element early.
  - Caps: a 2 MiB + 1 input → `RenderTooLarge`; 100001 elements → `RenderTooLarge`; nesting of 300 levels → depth ≤ 256 with all text kept.
  - The output never contains `nonce=`.
  - `render_kind`: `report.html` → html, `data.csv` → text, `a.svg` → image, `a.pdf` → pdf, `t.parquet` → document.
  - `RenderCache`: hit after put; eviction by count and by bytes.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_share_render.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(share): script-free static render for the annotation view`.

### Task 8: Annotation view and annotation routes in `share-public`

**Files:** Modify `herdr_engineering/control_plane/share_public.py` (module docstring security rules updated: the three annotation POSTs are the only routes that read a body); Create `herdr_engineering/control_plane/share_static/annotate.html` (template); Test extend `tests/control_plane/test_share_public.py`

**Interfaces:**
- `ShareConfig` gains `annotations: dict` (from `annotation_settings`) and `annotations_path: Path`, which defaults to `events_path.with_name("annotations.jsonl")`. `_share_public_server` fills both from the slice.
- `ShareServer` gains:
  - `annotate_user`, `annotate_share` and `annotate_global` `RateLimiter`s
  - `AnnotationLog.append(record: dict) -> bool` (the `EventLog` open/write rules, line ≤ 64 KiB, cap `ANNOTATIONS_MAX_BYTES`)
  - `render_cache: RenderCache`
  - `annotate_origin: str | None` (= `public_origin`; a test harness may override it)
- **The session check.** Every route below first checks the session against the current live lab state, else the uniform 404 plus a `denied` reason. The routes also need annotations enabled, a v2 session and `lab is not None`. A public share is always the 404.
- **`GET|HEAD /s/<token>/k/<session>/a/<path>`.** `<path>` must be in the state index (else 404 `not_in_bundle`). The response is `annotate.html` with these placeholders filled by `str.replace`, each exactly once and the content last:
  - `{{NONCE}}`, `{{CSRF}}`, `{{FILE_JSON}}` (the JSON-encoded file name, inside a `<script type="application/json" id="herdr-annotate-data" nonce>` block together with `kind`, `entry`, `expires_at` and `max_comment_chars`), `{{LIVE_HREF}}` (`<"../" × depth>f/<path>`, escaped), `{{CONTENT}}`.
  - What `{{CONTENT}}` holds depends on the kind:
    - `html` → `wrap_content("html", sanitize_html(...))` from `render_cache`. Over the cap, the message "This page is too large to show for annotation — comment on the whole file, or open the live view" and kind `document`.
    - `text` → `wrap_content("text", render_text(...))`, with the same cap.
    - `image`, `pdf` and `document` → empty: `annotate.js` builds them from `{{LIVE_HREF}}`.
  - The file is read through the same `_open_beneath` + size + sha256-from-index checks as `serve_bundle_file`. A mismatch is the 404.
  - Headers: the annotation-view CSP from Global Constraints with a fresh nonce, plus `X-Frame-Options: DENY`, `no-store`, `nosniff`, `no-referrer` and `noindex`. Sent with `cors=False`.
  - An `opened` event with `{"file", "mode": "annotate"}` for GET of the entry.
- **`GET /s/<token>/k/<session>/annotations`.** Returns 200 JSON `{"annotations": [own projection rows]}`, read from `<shares_root>/<share_id>/annotations/<sha256(identity)>.json` with `_open_beneath` (a missing file means `[]`). Headers: `no-store`, `nosniff`, `cors=False`.
- **`POST /s/<token>/k/<session>/annotations`** (create), **`POST .../annotations/<aid>/edit`** and **`POST .../annotations/<aid>/delete`.**
  - Every POST is refused unless all of these hold:
    - `Origin` equals `annotate_origin`
    - `X-Herdr-Annotate` passes `check_csrf`
    - `Content-Type` is `application/json` (parameters allowed)
    - there is exactly one `Content-Length`, 1–32768, and no `Transfer-Encoding`
  - Otherwise the response is the uniform 404 with reason `annotate_refused` (one code: no oracle).
  - Then:
    1. The three rate limiters are checked (429 `{"error": "rate_limited"}`).
    2. The body is read with the socket timeout and parsed by `parse_annotation_request` / `parse_edit_request` / `parse_delete_request` with `max_chars=annotations["max_comment_chars"]` (400 `{"error": <code>}`).
    3. **Create:** the anchor's `file` must be in the state index (400 `bad_file`). One line is appended: `{"v": 1, "op": "create", "annotation_id": "ann_" + token_urlsafe(16), "share_id", "identity", "email", "at", "anchor", "body"}`. Response: 201 `{"annotation": {"annotation_id", "anchor", "body", "status": "submitted", "created_at"}}`.
    4. **Edit / delete:** `<aid>` must match `ANNOTATION_ID_RE` (else 404 `annotate_refused`) and must be an `open` row in the caller's own projection. Otherwise the response is 409 `{"error": "not_editable"}`, and the panel says "This annotation can no longer be changed" or, for a row only just submitted, "Still saving — try again in a moment". One line is appended: `{"v": 1, "op": "edit" | "delete", "annotation_id", "share_id", "identity", "at", "body"?}`. Response: 202 `{"annotation_id", "status": "pending"}`.
    5. A failed append gives 503 `{"error": "unavailable"}`.
  - All POST responses are sent with `cors=False`.
- **`OPTIONS`** on any `a/` or annotations path → the uniform 404 (no preflight).
- `_route`:
  - accepts POST only for `session` and these three annotation paths
  - recognises `a/` paths
  - keeps `f/` (the live view) on `serve_bundle_file` unchanged
- `STATIC_FILES` gains `annotate.js`, `annotate.css` and the vendored pdf.js files of Task 10. Their size cap is per file: `STATIC_MAX_BYTES` stays 1 MiB, and `VENDOR_MAX_BYTES = 4 MiB` applies under `vendor/pdfjs/`. `_viewer_asset` accepts the one-level `vendor/pdfjs/<name>` allow-list only.

- [ ] **Step 1: Write failing tests** (raw `http.client`, as in the existing file)
  - **Annotation view**:
    - Each kind (HTML, text, PNG, PDF, parquet) → 200 with the CSP holding a 24-character nonce that differs between two requests.
    - The HTML body has `<template shadowrootmode="open">` and our nonce'd `annotate.js`, and has no CORS header.
    - A bundle `index.html` containing `<script>window.x=1</script><iframe src=a></iframe>` comes back with neither.
    - A 2 MiB + 1 HTML file gets the too-large page.
    - A v1 session, annotations disabled, a public share, an expired session, a revoked share or a path not in the index → the uniform 404.
  - **The live view is unchanged:** `f/index.html` bytes, headers, CSP and ETag are identical to `main` (golden comparison).
  - **POST create**:
    - A valid create appends exactly one line, returns 201, and the line holds the session's email and identity — never anything taken from the body, even when the body sends `email`.
  - **POST refusals** — each writes no line:
    - Origin `null`, missing, or of another host
    - missing or wrong `X-Herdr-Annotate`, or a CSRF token of another session
    - `text/plain`
    - two `Content-Length` headers, `Transfer-Encoding: chunked`, 32769 bytes
    - a v1 session, annotations disabled, a public share, an expired session, a revoked share
  - The 61st POST in a minute by one identity → 429.
  - A file not in the index → 400 `bad_file`.
  - **Edit / delete**:
    - Edit of an id in the caller's projection with status `open` → 202 and an `op: "edit"` line.
    - An id in another identity's projection → 409.
    - An id that is not well-formed → 404.
    - A body with an extra `anchor` key → 400.
  - OPTIONS on each path → 404.
  - The GET returns only the caller's projection rows.
  - `annotations.jsonl` at `ANNOTATIONS_MAX_BYTES` → 503.
  - The stdlib request log is still silent: no path, CSRF token or nonce reaches a log record.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_share_public.py tests/control_plane/test_share_lab.py tests/control_plane/test_share_render.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(share): annotation view and annotation routes`.

### Task 9: Edge nginx: a dedicated share-host server block

**Files:** Modify `deploy/control-plane/collab-edge/share-http.conf` (now holds the share `server {}` block), `share-location.conf` (in the MCP server block: the `308` to the share host), `deploy/control-plane/compose.collab.yaml` (mount `${HERDR_SHARE_CERT_DIR}` at `/etc/acme-share:ro`), `herdr_engineering/fleet_config_cli.py` (`render_edge_conf` substitutes `@share_host@` from `control_plane.share.public_url`); Test extend `tests/test_share_deploy_config.py` (the `render_edge_conf` tests)

**Interfaces:**
- `render_edge_conf(template, *, share: bool, share_host: str | None = None, snippets_dir=None)`. With `share` true, `share_host` is required: a validated lower-case hostname, port allowed. Every `@share_host@` in the snippets is replaced. Disabled, the output is byte-identical to the template, as today.
- **`share-http.conf`** keeps today's zones, upstream and maps, and adds:
  - `limit_req_zone $remote_addr zone=share_annotate:10m rate=5r/s;`
  - `$share_method_ok` entries `"~^POST:/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations(/ann_[A-Za-z0-9_-]{22}/(edit|delete))?\z" 1;`
  - **a `server { listen 8443 ssl proxy_protocol; server_name @share_host@; … }` block** with:
    - `ssl_certificate /etc/acme-share/fullchain.pem` and the matching key
    - HSTS and the `$hcn_from_edge` guard copied from the MCP block
    - `proxy_set_header Host`, `X-Forwarded-*` and `Origin` as in the MCP block
    - **flat** locations, nothing nested:
      - `location /s/` (a plain prefix, not `^~`): today's `/s/` body, with the method/body guards, `client_max_body_size 1k` and `proxy_pass_request_body off`.
      - `location ~ ^/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations(/ann_[A-Za-z0-9_-]{22}/(edit|delete))?$`: a regex, so it beats the plain prefix. It has its own method guard (GET and POST only, else `return 404`), `access_log off`, `error_log /dev/stderr crit`, `client_max_body_size 32k`, `proxy_pass_request_body on`, `limit_req zone=share_annotate burst=30 nodelay`, `limit_conn share_conn 16` and `proxy_pass http://share`.
      - `location / { return 404; }`
- **`share-location.conf`** (spliced into the MCP host's block) becomes `location ^~ /s/ { access_log off; return 308 https://@share_host@$request_uri; }`. Links issued earlier keep working.

- [ ] **Step 1: Write failing tests** on the rendered config:
  - The rendered config has a `server_name artifacts.example.test;` block, and the annotations regex location sits at brace depth 2 inside it, not nested in `location /s/`.
  - The annotations location has `proxy_pass_request_body on`, `client_max_body_size 32k` and `share_annotate`.
  - `location /s/` still has `proxy_pass_request_body off` and the body rule.
  - The MCP host's block holds the `308` and no `proxy_pass http://share`.
  - With sharing disabled the rendered config is still byte-identical to the template.
  - `share=True` without `share_host` raises.
  - If `nginx` is on PATH, `nginx -t` passes on the rendered config, with dummy certificates generated into a temp dir (skip otherwise).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(edge): share host server block and annotation posts`.

### Task 10: `annotate.js`: the annotation panel, capture per kind, pdf.js

**Files:** Create `herdr_engineering/control_plane/share_static/annotate.js`, `annotate.css`, `share_static/vendor/pdfjs/{pdf.min.mjs,pdf.worker.min.mjs,LICENSE}` (from `pdfjs-dist@4.10.38`, sha256 of each recorded in `share_static/vendor/pdfjs/SHA256SUMS`); Create `tests/playwright/share-annotations-server.py`, `tests/playwright/share-annotations.test.mjs`, `tests/control_plane/test_share_vendor.py`

**Interfaces:**
- `annotate.js` is an ES module loaded by `<script type="module" nonce>`. It reads `#herdr-annotate-data` and the CSRF value from `<meta name="herdr-annotate-csrf">`. It touches no cookie and no storage, and its only network calls are to `../annotations` (relative, built from `location.pathname`) and to the `f/` file for images and PDFs.
- **Panel.** A `<aside data-annotate-panel>` hosting a **closed** shadow root, built with `createElement`/`textContent` only. It is stacked below the content at ≤ 640px. It holds:
  - the header "Annotating a static copy"
  - a link "Open the live version" → `LIVE_HREF`
  - the expiry note
  - the current anchor: quote in a `<blockquote>`, "Page N" plus quote and/or region for PDF, a region box for images, "Whole file" otherwise. A "Whole file instead" button resets the anchor to `document`.
  - a `<textarea maxlength=max_comment_chars>` with a live counter and a Submit button
  - "Your annotations": status Open / Addressed in vN / Dismissed / Submitted / Edited. Open rows have Edit (inline textarea, Save / Cancel) and Delete (with `confirm()`).
- **Capture by kind:**
  - **HTML / text.** On `selectionchange` (debounced 150 ms) inside `#herdr-content`'s shadow root, `getComposedRanges({shadowRoots: [root]})` gives the range. Where that API is missing, it falls back to `getSelection().toString()` with the context taken from `root.textContent`. The result:
    - `exact` = the selection text clipped to 1000 characters
    - `prefix` / `suffix` = 64 characters of the shadow root's `textContent` around it
    - `file` = `FILE`
  - If the browser did not attach the declarative shadow root (`!host.shadowRoot`), the script attaches one and moves the `<template>` content into it.
  - **Image.** An `<img src=LIVE_HREF>` inside a positioned box. Pointer drag draws a box, and the region is normalised to the image's natural size in [0, 1].
  - **PDF.**
    - The script imports `vendor/pdfjs/pdf.min.mjs` and sets `GlobalWorkerOptions.workerSrc` to the vendored worker.
    - It calls `getDocument({url: LIVE_HREF, isEvalSupported: false, enableXfa: false, disableAutoFetch: true})` and renders only visible pages (IntersectionObserver) to `<canvas>` with a text layer, using `annotationMode: AnnotationMode.DISABLE`, so no link annotations are drawn.
    - A selection in a page's text layer gives `pdf_page` with `exact`. A drag with Alt or the "Mark region" toggle gives `region`.
    - A load error shows "This PDF could not be drawn" and keeps whole-file comments.
  - **Document.** The file name, a link to the live view or download, and a whole-file comment.
- **Submit** sends POST JSON with `X-Herdr-Annotate: <csrf>`, `credentials: "omit"` and `cache: "no-store"`.
  - 201 or 202 → update the list.
  - 400 → a fixed message per code.
  - 409 → the not-editable or still-saving text (Task 8).
  - 429 → "Too many annotations — wait a minute".
  - 503 → "Annotations are unavailable right now — try again later".
  - 404 → "Your session ended — open the link again", keeping the text in the box.
  - The button is disabled while the request is in flight.
- **Harness** (`share-annotations-server.py`) starts a real `ShareServer` on 127.0.0.1:
  - It builds `LabSettings` directly with a stub authenticator and a fixed secret, sets `server.annotate_origin` and `public_origin` to its own `http://127.0.0.1:<port>`, and enables annotations.
  - It seeds lab shares:
    - an HTML bundle with `index.html` and `page2.html`, plus a hostile `evil.html` that has scripts, an iframe, `* { display: none !important }` and a `position: fixed` full-screen overlay
    - a 3-page PDF
    - a PNG
    - a `.parquet` file
    - one public share
  - It prints `PORT=<n>` and a JSON line of v2 session base URLs for the `a/` and `f/` views, plus the public share URL.
  - It exposes `GET /__lines` → the parsed `annotations.jsonl`, and `POST /__revoke/<share>`, both on a separate port. It also writes a projection file on demand (`POST /__project`) so the edit and delete UI has open rows.

- [ ] **Step 1: Write the Playwright tests**
  - HTML annotation view: select a sentence; the blockquote shows it; submit the hostile comment `<img src=x onerror=window.__x=1>` → the list shows it literally, `window.__x` is undefined, and `/__lines` has one line with the quote, prefix and suffix.
  - Follow the in-content link to `page2.html` → the URL is `a/page2.html`, and the next annotation's file is `page2.html`.
  - `evil.html`: no dialog or script effect, no `iframe`/`frame`/`object`/`embed` element in the DOM, the panel is visible and clickable (`* { display: none }` and the overlay do not cover it), and selection and submit still work.
  - **Live view → no forgery:** open `f/index.html` top-level, then `page.evaluate` a `fetch` POST to `../annotations` with JSON and a guessed header → it fails, and a `fetch` of `../a/index.html` cannot read the body (CORS) → `/__lines` is unchanged.
  - The counter blocks the character after `max_comment_chars`.
  - PDF: page 3 selection → `pdf_page` with `page: 3` and the quote; a marked region → `pdf_page` with a `region` in [0, 1].
  - Image: drawing a box submits `image_region` with values in [0, 1].
  - Parquet: whole-file comment → `document`.
  - Edit an open own row → an `op: "edit"` line; Delete → an `op: "delete"` line after confirm.
  - The public share URL has no `a/` route (404), and `POST` to a fabricated `k/` path is 404.
  - Revoke mid-session (`/__revoke`) → submit shows the session-ended message and keeps the text.
  - `#annotate`: opening `/s/<token>/#annotate` through the stub sign-in lands on `a/<entry>`; without the hash it lands on `f/<entry>`, as today.
  - Viewport 320px: no horizontal scroll and the panel is below the content.
- [ ] **Step 2: Write** `test_share_vendor.py`: every file under `vendor/pdfjs/` matches `SHA256SUMS`, `LICENSE` is Apache-2.0, and no other file is present.
- [ ] **Step 3: Run** `node --test tests/playwright/share-annotations.test.mjs` and the vendor test → FAIL.
- [ ] **Step 4: Implement** `annotate.js` and `annotate.css`, and vendor pdf.js. Download the tarball from the npm registry, check its `integrity` against `npm view pdfjs-dist@4.10.38 dist.integrity`, and copy only the three files.
- [ ] **Step 5: Run** both, plus `python3 -m pytest tests/control_plane/test_share_public.py -q` → PASS.
- [ ] **Step 6: Commit** `feat(share): annotate from the lab annotation view`.

### Task 11: Owner annotation API and web-client methods

**Files:** Create `herdr_engineering/control_plane/artifact_annotation_api.py`; Modify `herdr_engineering/control_plane/api.py` (call `route_annotations` right after `route_share`), `packages/herdr-web-client/client.js`; Test `tests/control_plane/test_artifact_annotation_api.py`, `packages/herdr-web-client/artifact-annotations.test.js`

**Interfaces:**
- `def route_annotations(handler, server, parts, resource) -> bool`:
  - `GET /api/v1/artifacts/{id}/annotations?status=open|addressed|dismissed|withdrawn|all&limit=1..500&before=<cursor>` → `{"artifact_id", "annotations": [...], "next": cursor | null}`, using the `list_for_artifact` fields plus `author_email`, `author_role` and `share_id`.
  - `POST /api/v1/artifacts/{id}/annotations/{aid}/dismiss` with body `{}` → `{"annotation": {...}}`.
  - Both need the `share` permission (non-disclosing 404) and the same cross-origin POST refusal as `route_share`. A body with unknown keys → 400. `not_open` → 409.
  - Any POST answered early closes the connection, as in `route_share`.
- Client: `listArtifactAnnotations(id, { status = "open", before, signal } = {})` and `dismissArtifactAnnotation(id, annotationId, { signal } = {})`, with ids passed through `encodeURIComponent`.

- [ ] **Step 1: Write failing tests**
  - Without `share` → 404.
  - The list filters by status, including `withdrawn`; limit 0 or 501 → 400; a malformed cursor → 400; paging returns `next`.
  - Dismiss on an open annotation → 200 and the next list shows it as dismissed with an `annotation_dismissed` share event.
  - Dismiss of an addressed or withdrawn annotation → 409.
  - A cross-origin POST → 403.
  - Client: exact paths, methods and bodies.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(annotations): owner list and dismiss API`.

### Task 12: Feedback panel in the artifact detail view

**Files:** Create `packages/artifact-feedback-panel.js`, `packages/artifact-feedback-panel.test.js`; Modify `packages/artifacts-view.js` (mount it in `renderArtifactDetail` and `renderReportReader` next to `mountSharePanel`; no new iframes), `packages/artifact-share-panel.js` (`renderEvents` labels the five new kinds; where a lab link is shown once at approve, also show "Annotation link" = the URL + `#annotate`)

**Interfaces:**
- Consumes: Task 11 client methods.
- Produces:
  - `export function renderAnchor(anchor)`:
    - `text_quote` → `…prefix<mark>exact</mark>suffix…`, all escaped
    - `pdf_page` → `Page N`, plus the quote, plus `region x%, y% (w% × h%)` when present
    - `image_region` → `Region x%, y% (w% × h%)`
    - `document` → `Whole file`
    - always followed by `in <code>file</code>`
  - `export function renderAnnotationRow(annotation, { artifactId, busy })` returns escaped HTML:
    - the author email plus an "owner" badge when `author_role === 'owner'`
    - version `vN`, the anchor, and an "edited" marker when `edited_at` is set
    - the body in `<p style="white-space:pre-wrap;overflow-wrap:anywhere">`
    - the status
    - a link to `addressed_in_version`, built with the version-link format the report reader writes (`artifacts-view.js` ~line 212)
    - a Dismiss button only when `status === 'open'`
  - `export function mountFeedbackPanel(container, client, artifactId, { signal }) -> { dispose() }`:
    - status filter Open / Addressed / Dismissed / Withdrawn / All (default Open), plus "Load more" while `next` is set
    - per-section `<herdr-error>` plus Retry
    - Dismiss asks `confirm()`, disables the button in flight and refreshes the list
  - If plan 3's Share card has a tab bar, mount into a "Feedback" tab panel. Otherwise mount as a `<section data-artifact-feedback>` card after the Share card.

- [ ] **Step 1: Write failing tests**
  - Hostile email, body, quote and file name are escaped.
  - Open rows have Dismiss; addressed rows link to vN and have no Dismiss.
  - The owner badge and the edited marker are shown.
  - A 4000-character unbroken body gets the wrap style.
  - Empty state "No annotations yet. Lab viewers can annotate shared versions from the annotation link; public links cannot."
  - `renderEvents` shows `annotated` as "annotated" and `annotation_withdrawn` as "annotation deleted by its author".
  - The approve result for a lab share shows the annotation link ending in `#annotate`; a public share shows none.
- [ ] **Step 2: Run** `node --test packages/artifact-feedback-panel.test.js` → FAIL.
- [ ] **Step 3: Implement** and mount.
- [ ] **Step 4: Run** `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(web): feedback panel for artifact annotations`.

### Task 13: MCP tools `artifact_annotations` and `artifact_annotation_resolve`

**Files:** Modify `herdr_engineering/control_plane/collab_mcp.py`, `collab_bridge_files.py` (intent kind `annotation_resolve`), `collab_intents.py` (dispatch), `collab_artifacts.py` (`CollabArtifactBridge.annotations` / `.resolve_annotations`), `collab_mirror.py` (writes `artifacts/<id>/annotations.json`), `herdr_engineering/control_plane/worker.py` (`AnnotationHousekeeper` asks the mirror to refresh touched artifacts), `packages/collab-guide.md`, `docs/operations/collaboration-network-guide.md`, `skills/herdr-artifact/SKILL.md` (tool list only); Test `tests/control_plane/test_collab_mcp_annotations.py`, `tests/control_plane/test_annotation_resolve_intent.py`

**Interfaces:**
- Consumes: the "Consumed from plan 3" names, plus `AnnotationStore.open_for_agents` and `AnnotationStore.resolve` (Task 3).
- **Mirror file** `<mirror>/artifacts/<artifact_id>/annotations.json` = `{"artifact_id", "generated_at", "annotations": open_for_agents(...)}`.
  - It holds at most 1 MiB, newest first; when cut, it carries `"truncated": true`.
  - `CollabMirror.publish_annotations(artifact_id, rows)` writes it with `atomic_write_json`, only for artifacts that hold a live grant.
  - The `AnnotationHousekeeper` tick calls it for touched artifacts (this covers edits and withdrawals), and the applier calls it after a resolve.
  - `sync()` removing an ungranted artifact's directory also removes this file.
  - Each annotation carries `exact`/`prefix`/`suffix` (or page and region) and `file` inside `anchor`, plus `version_id` and `edited_at`.
  - The anchors refer to the **static render**: the tool description says so. That lets an agent locate an annotation in head even when the annotated version is older.
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
    - anchors quote a static, script-free rendering of the file;
    - integrate the feedback with `artifact_commit`, then resolve with the version id that commit returned;
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
  - On a withdrawn id → skipped `not_open`.
  - With a grant revoked after the intent was written → `rejected` with "no write grant".
  - Replaying the same idempotency key → the same result, no second update.
  - The mirror file is rewritten after resolve and no longer lists the resolved ids. After an author's delete line is ingested, it no longer lists that id.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**, and document both tools in `packages/collab-guide.md` and its `docs/operations` mirror: what they return, the untrusted notice, the static-render anchors, and the commit-then-resolve workflow.
- [ ] **Step 4: Run** the new tests plus `tests/control_plane/test_collab_public.py` and the other tests importing `collab_mcp` (`grep -rln collab_mcp tests`) → PASS.
- [ ] **Step 5: Commit** `feat(collab): annotation tools for granted agents`.

### Task 14: End-to-end owner flow, the no-frames guard, spec text and deploy notes

**Files:** Modify `tests/playwright/artifact-shares-server.py` (in-memory annotation routes with the Task 11 shapes), `tests/playwright/artifact-shares.test.mjs` (or a new `artifact-feedback.test.mjs` wired the same way), `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md` (section D), `docs/operations/` share deploy doc (the one that documents `control_plane.share`; find it with `grep -rl "control_plane.share" docs/operations`); Create `tests/control_plane/test_share_no_frames.py`

- [ ] **Step 1: Write the Playwright test**
  - The artifact detail page shows the Feedback section with three seeded annotations: one open with a hostile body, one addressed in v3, and one open with `edited_at`.
  - The hostile body is literal and `window.__x` stays undefined.
  - The addressed row links to v3, and the edited row shows "edited".
  - Dismiss → the row leaves the Open filter and appears under Dismissed; a double click sends one POST (the stub counts).
  - A server 500 shows the error with Retry, and Retry recovers.
  - 320px viewport: no horizontal scroll.
- [ ] **Step 2: Write** `test_share_no_frames.py`:
  - **(a)** Every file in `share_static/` except `vendor/` contains none of `<iframe`, `<frame`, `<object`, `<embed`, `"iframe"`, `'iframe'` or `postMessage` (case-insensitive). So do `packages/artifact-feedback-panel.js` and the Task 12 diff of `artifact-share-panel.js`.
  - **(b)** Using the Task 8 test server, the body of every non-content response is fetched: the viewer page, `/s/callback`, the static assets, the annotation views of every kind including a bundle full of frames, and the annotations JSON. Lower-cased, none contains `<iframe`, `<frame`, `<object` or `<embed`.
  - **(c)** `sanitize_html` on a corpus of frame and plugin markup (`<IFRAME`, `<iframe/src=…>`, `<frameset><frame>`, `<object><embed>`, `<svg><foreignObject><iframe>`) yields none of those strings.
  - The live view (`f/`) serves raw content and is excluded from (b) by design.
- [ ] **Step 3: Run** `npm run test:ui` for the file and the new pytest → FAIL, then implement the stub routes until PASS. Fix product code if the test reveals a defect.
- [ ] **Step 4: Update the spec**, section D: replace the shell / injected script / `postMessage` capture with the annotation view (the static render, nonce CSP, shadow root, CSRF), add the edit/delete rule, the `withdrawn` status, the raised limits, the `pdf_page` region and the share host. Also add a "No frames (operator 2026-10-30)" line to its constraints.
- [ ] **Step 5: Write the deploy notes** into the share deploy doc. These are operator steps; the implementer does not run them:
  1. DNS: `artifacts.metabolomics.us` → the public edge.
  2. Add the hostname to the cubert SNI stream edge in `networking/cubert-ansible`, routed to the collab edge like `localdev.mcp.metabolomics.us`.
  3. Certificate: `deploy/control-plane/collab-sync.sh install --cert-name artifacts.metabolomics.us --cert-dir <dir>`, and set `control_plane.compose.HERDR_SHARE_CERT_DIR: <dir>`.
  4. Cognito app client `cognito_web_client`: add the allowed callback URL `https://artifacts.metabolomics.us/s/callback` and the allowed sign-out URL `https://artifacts.metabolomics.us/`. Keep the old host's callback until its links have expired.
  5. YAML: set `control_plane.share.public_url: https://artifacts.metabolomics.us`, plus optionally `owner_emails` and `annotations` overrides.
  6. Redeploy collab-edge, share-public and the control plane, then apply migration 0045.
  7. Smoke-check:
     - an old MCP-host link answers `308` to the new host
     - a lab link signs in
     - `#annotate` opens the annotation view
     - a test annotation reaches the Feedback panel
- [ ] **Step 6: Run** the UI gates for the touched surfaces (`npm run test:ui-gates`) and update intentionally changed baselines by the repo's procedure. A baseline already red on main is not this plan's to touch.
- [ ] **Step 7: Run** the whole control-plane suite (`python3 -m pytest tests/control_plane -q`), `tests/test_share_deploy_config.py` and `npm test` → PASS.
- [ ] **Step 8: Commit** `test(annotations): owner feedback end-to-end, no-frames guard, spec and deploy notes`.

---

## Self-review notes

- **Spec coverage (section D, as amended by the Decisions):**
  - who may annotate → Tasks 6, 8
  - anonymous/public refused → Tasks 8, 10
  - anchors (HTML/text quote, PDF page with optional quote and region, image region, other → whole file; parquet excluded) → Tasks 1, 7, 10
  - capture without frames: static render, nonce CSP, shadow root → Tasks 7, 8, 10
  - annotation UI and the comment box → Task 10
  - session-authenticated, CSRF-checked, size-capped, rate-limited POST → Tasks 6, 8, 9
  - plain text never HTML → Tasks 1, 10, 12, 13
  - database-free append plus worker ingest → Tasks 2–4
  - statuses (`open`, `addressed`, `dismissed`, `withdrawn`) and `addressed_in_version_id` → Task 3
  - edit/delete own annotations while open → Tasks 1, 3, 4, 8, 10
  - size and rotation rules of the annotations file → Tasks 2, 4, 8
  - owner Feedback tab with Dismiss and version link → Tasks 11, 12, 14
  - annotators see their own annotations and their status → Tasks 4, 8, 10
  - MCP `artifact_annotations` (untrusted) and `artifact_annotation_resolve` → Task 13
  - a new revision never changes a live link → no code changes share state (Global Constraints)
  - owner annotates through the lab link with their email auto-added → Task 4
  - share host from YAML, MCP unchanged → Tasks 5, 9; deploy steps → Task 14
  - no frames anywhere → Global Constraints, Task 7 (sanitizer), Task 14 (guard test)
- **Removed from the earlier draft:**
  - the shell route `k/<session>/`
  - the sandboxed iframe and the PDF frame
  - `postMessage` intake
  - HTML script injection into bundle files and its `-a1` ETag
  - the `__PARENT_ORIGIN__` substitution
  - `frame-ancestors 'self'` and `frame-src 'self'`
  - the nested nginx location
- **Out of scope:**
  - "share latest" convenience (spec: follow-up)
  - members reading their own feedback via MCP (plan 5; `artifact_annotations` already serves granted agents)
  - highlighting existing annotations inside the content
  - annotating the live (scripted) view
  - a markdown renderer
  - an inline SVG allow-list
  - replacing the pre-existing dashboard preview iframes in `packages/artifacts-view.js` (lines ~158 and ~419; see the operator questions)
- **Names used across tasks:** `annotation_anchor.{parse_annotation_request, parse_edit_request, parse_delete_request, ANNOTATION_DEFAULTS, ANNOTATION_ID_RE}`, `AnnotationInputError`, `JsonlIngestor`, `AnnotationStore.{ingest, list_for_artifact, dismiss, resolve, own_rows, open_for_agents, touched_since}`, `AnnotationHousekeeper`, `write_own_projection`, `annotation_settings`, `share_session.{csrf_token, check_csrf}`, `share_render.{render_kind, sanitize_html, render_text, wrap_content, RenderCache, RenderTooLarge}`, `render_edge_conf(..., share_host=)`, `route_annotations`, `listArtifactAnnotations`, `dismissArtifactAnnotation`, `mountFeedbackPanel`, `apply_annotation_resolve`, intent kind `annotation_resolve`, JSONL `op` values `create | edit | delete`, file `annotations.jsonl`, mirror file `annotations.json`, routes `k/<session>/a/<path>` and `k/<session>/annotations[/<aid>/(edit|delete)]`. These are consistent across tasks.
- **Proportion:** 14 tasks. Task 7 (the sanitizer) carries an explicit allow-list because tests alone do not determine it. Everything else is signatures, values and tests.
- **Operator-only questions:**
  1. **Old links.** Links already issued under the MCP host get a `308` to `artifacts.metabolomics.us` (the plan's default). Keep that, or should the MCP host stop serving `/s/` entirely (old links break)?
  2. **Landing mode.** A lab link lands on the live view as today, and the annotation view is reached with the `#annotate` link the Share card shows at approve. Should lab links land on the annotation view, or on a two-button chooser, instead?
  3. **Existing iframes.** `packages/artifacts-view.js` (~158 and ~419) already uses iframes for the owner dashboard's HTML/PDF previews. Does "never an iframe anywhere" cover those too? That would be a separate change, not in this plan.
  4. **Vendoring pdf.js** (Apache-2.0, pinned `pdfjs-dist@4.10.38`, about 1.5 MB) into `share_static/vendor/`: OK? The alternative is page-number-only PDF anchors with no rendering.
