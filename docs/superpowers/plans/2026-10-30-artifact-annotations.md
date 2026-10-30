# Artifact annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship plan 4 of the artifact sharing / collaboration design.
- Signed-in lab viewers and the owner can annotate a lab-shared artifact.
- Each annotation is bound to the share's pinned version plus an anchor.
- Annotators can edit or delete their own annotations while they are still open.
- The owner sees and dismisses annotations in a Feedback section of the Share card.
- A granted agent reads open annotations and marks them addressed through two MCP tools.
- Anonymous and public visitors get no annotation surface at all.
- The plan also removes every remaining iframe from the owner dashboard.

**Architecture:**
- **No frames anywhere.** No `<iframe>`, `<frame>`, `<object>` or `<embed>` exists anywhere in our HTML or JS, and there is no cross-document `postMessage`. This covers the share host, the annotation view and the owner dashboard (operator decision 2026-10-30). Each view is a top-level document.
- **Live and annotation views of one lab share, all on the share host** (`control_plane.share.public_url`, for example `https://artifacts.metabolomics.us`). The old `/s/` location on the MCP host is removed. A lab link lands **directly on the live view for every file type** (operator decision 2026-10-30).
  - **Live view, HTML.** This is today's raw route `GET /s/<token>/k/<session>/f/<path>`. The bundle file runs top-level with the existing `sandbox allow-scripts allow-downloads …` CSP (`share_public._CSP_TEMPLATE`/`sandbox_csp`), which is unchanged, so its scripts run in an opaque origin.
    - For a **lab** share with annotations enabled, a `text/html` response to a **document navigation** (`Sec-Fetch-Dest: document`) carries one injected **mode-switch link**. The link is a script-free `<a>`, fixed-position and styled inline, with `rel="noopener"`, pointing to `a/<path>`. It is inserted before the last `</body>`, or appended when there is none.
    - Every other `f/` response stays byte-for-byte unchanged: public shares, non-HTML files, and sub-resource or `fetch` requests from content.
  - **Live view, other kinds.** `GET /s/<token>/k/<session>/v/<path>` serves image, PDF, text and other kinds. It is a top-level page that `share-public` builds from our own template: a native `<img>`, pdf.js canvases, an escaped `<pre>`, or a download link. None of these kinds has anything to run. The template's server-rendered **mode bar** (`Live | Annotate`) sits outside the content's shadow root. `v/` of an HTML file is the 404, because HTML's live view is `f/`.
  - **Annotation view** (new). `GET /s/<token>/k/<session>/a/<path>` uses the same template for every kind. Its mode bar shows `Annotate` selected, and its `Live` link goes to `f/<path>` for HTML or `v/<path>` otherwise. The annotation panel is present.
  - Both of our template views (`v/` and `a/`) share these properties:
    - The content is a **script-free static render**. HTML is cleaned by the vetted `nh3` (ammonia) sanitizer through a thin adapter. Text becomes an escaped `<pre>`, images a native `<img>`, and PDFs are drawn by a vendored, pinned pdf.js onto canvases. Other types get a whole-file comment only.
    - Our `annotate.js` runs as the only script, allowed by a per-response CSP nonce.
- **How hostile content is contained.**
  - Content scripts never run in our views. The sanitizer strips them, and `script-src 'nonce-…' 'strict-dynamic'` has no `'self'` in addition.
  - In the HTML live view (`f/`), content scripts run in an opaque origin. The injected link carries only the `a/` path, which the page already knows from `location`, and never the CSRF token. Their requests carry `Origin: null`, and they cannot read our views, which are sent without CORS.
  - So hostile content can neither run next to `annotate.js` nor forge an annotation POST.
  - In our views, the content sits in a **declarative shadow root** (`<template shadowrootmode="open">`). Its CSS cannot restyle the mode bar or the panel, its ids cannot clobber our globals, and `contain: paint` keeps its `position: fixed` boxes inside the content area.
- **Owner dashboard.** Every preview iframe goes away.
  - HTML previews (the plain artifact, the report preview and the legacy report outputs) use the same nh3 adapter through authenticated owner routes, mounted in a shadow root.
  - PDFs are drawn by the same vendored pdf.js on canvases. Images use a native `<img>`.
  - A link "Open live view in a new tab" opens a new owner route served top-level under a sandbox CSP.
- **Session.**
  - The session stays the HMAC path segment `k/<session>/`. There are no cookies in `share-public`.
  - Every session is **v2**, which binds the verified email. v1 is removed, with no compatibility path.
  - Annotation writes also need:
    - the exact share `Origin`
    - `Content-Type: application/json`
    - an `X-Herdr-Annotate: <csrf>` header. The CSRF token is an HMAC of the session and is delivered only inside the annotation view, which is not readable cross-origin.
  - The custom header forces a preflight, and `OPTIONS` stays the uniform 404.
- **Storage path.**
  - `share-public` stays database-free. It appends `create` / `edit` / `delete` records to `annotations.jsonl`.
  - The worker ingests them into `artifact_annotations`. It derives artifact and version from the share row and enforces authorship and the `open` state.
  - It writes per-viewer read-back projections onto the shares volume, and the open-annotation list into plan 3's MCP mirror.
- `annotation_resolve` is an intent kind that this plan registers through plan 3's intent-kind handler registry. The control-plane worker is the authority that applies it.
- **Anchors are against the static render:**
  - `text_quote` for HTML and text
  - `pdf_page` with an optional quote and an optional region, for PDF
  - `image_region` for images
  - `document` for everything else, or as a fallback

**Tech Stack:**
- Python 3.12 (`http.server`) for `share_public.py`. The new `share_render.py` wraps **`nh3`**, a new core dependency. asyncpg in the control plane and worker.
- Plain ES-module JS in `packages/` and `share_static/`.
- pdf.js comes from `pdfjs-dist@4.10.38` (Apache-2.0, released January 2025). It is vendored once by `scripts/vendor-pdfjs.sh` under `share_static/vendor/pdfjs/` and served by both `share-public` and the dashboard.
- Tests: node:test for unit tests, Playwright (Chromium) for UI, and pytest with the Postgres fixtures of the existing `test_share_*` / `test_artifact_*` tests.
- Postgres migrations live in `deploy/control-plane/migrations`, and the edge nginx in `deploy/control-plane/collab-edge`.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md`, section D, plus the "Second builder check" bullet "Owner annotations".
- Section D's "shell / injected script / postMessage" capture is superseded by the operator decisions below. Task 16 updates the spec text.
- Share-link mechanics: `docs/superpowers/specs/2026-10-30-artifact-share-links-design.md`.
- Code:
  - `herdr_engineering/control_plane/{share_public,share_session,share_lab,share_bundle,artifact_shares,artifact_share_api,artifact_report_api,worker,api}.py`
  - `share_static/`
  - `packages/artifacts-view.js`
  - `herdr_engineering/fleet_config_cli.py` (`SHARE_KEYS`, `SHARE_SLICE_KEYS`, `share_values`, `render_edge_conf`, the `@share-location@` splice)
  - `herdr_engineering/cli.py` (`_share_public_server`, `share_settings_config` call sites, `_cmd_cp_worker`)

## Decisions (operator, 2026-10-30; binding)

The project is in **alpha**. Nothing has been issued, so everything is replaceable and there are no backward-compatibility shims: no v1 sessions, no redirects, and no legacy-link handling.

1. **No frames, anywhere, ever.**
   - No `<iframe>`, `<frame>`, `<frameset>`, `<object>`, `<embed>` or `srcdoc` in any HTML or JS we serve, and no cross-document `postMessage`. pdf.js's internal messages to its own worker are not cross-document and are allowed.
   - This applies to the owner dashboard too. Its three preview sites in `packages/artifacts-view.js` are replaced in Task 13:
     - `showPreview` (~line 152), which serves both the report preview and the legacy HTML outputs
     - the PDF and HTML iframes at ~line 419
   - **Tradeoff, accepted:**
     - Annotations apply to the static render, not to the running app.
     - Interactive content (JS-built charts, apps) cannot be annotated in place. Its viewer leaves a whole-file comment or annotates whatever static text survives sanitizing.
     - Text quotes refer to the sanitized text, so text that a script adds at run time is not quotable.
     - Inline `<svg>`/`<math>`, `<canvas>`, `<video>`, `<audio>` and every frame or plugin element are removed with their content. The library cannot leave a placeholder in their place. Instead, every static render carries the fixed note "Scripts, frames and embedded media are not shown in this static copy".
     - Relative `url(...)` references inside a `<style>` element are not rewritten, because nh3 does not filter style text, so such background images do not load in the static copy. References in `style` attributes and in linked stylesheets do resolve.
     - `.md` files are `application/octet-stream` today (`share_bundle._CONTENT_TYPES`), so they get a whole-file comment only. A markdown renderer is out of scope.
2. **Share host, with no legacy links.**
   - The share host comes from the new YAML key `control_plane.share.public_url`. MCP stays on `control_plane.collab.public_url`.
   - When `share.enabled` is true, `share.public_url` is **required** and must differ from the collab host. The key follows the existing key's naming and validation: an https origin with an empty path.
   - The MCP host's `/s/` location (`share-location.conf` and its `@share-location@` splice) is **deleted**. There is no redirect, and links issued under the MCP host stop working.
   - The operator deploys. The Cognito callback and sign-out URLs, DNS, the certificate and the SNI hostname are deploy steps (Task 16). Only the new host's callback is registered.
3. **Owner emails** come from YAML `control_plane.share.owner_emails`, a list that defaults to `[]`. The API and the worker read it, and it is never part of the share slice.
4. **Annotations are on for every lab share.** There is no per-share opt-in, and public shares never get annotations. The global kill switch is `control_plane.share.annotations.enabled`. When it is off, the `Annotate` side of the switch is not rendered and `a/` is the 404.
5. **Annotators may edit and delete only their own annotations, and only while the status is `open`.** Once an annotation is `addressed` or `dismissed`, it is frozen.
   - Delete is a soft delete: the status becomes `withdrawn`, and an audit event is kept.
   - The worker is the authority, because `share-public` cannot verify authorship durably.
6. **Limits are configurable** under `control_plane.share.annotations`, with defaults raised for generous capacity:

   | Key | Default | Meaning |
   |---|---|---|
   | `per_user_per_minute` | 60 | creates, edits and deletes per user |
   | `per_share_per_minute` | 600 | per share |
   | `global_per_minute` | 3000 | across all shares |
   | `per_user_per_day` | 2000 | creates per user |
   | `max_per_share` | 20000 | annotations per share |
   | `max_comment_chars` | 4000 | 1–4000; 4000 is also the DB CHECK ceiling |

7. **Image and PDF region anchors are kept.** `image_region` is used for images, and `pdf_page` takes an optional `region` on that page.
8. **The raw file's (`f/`) sandbox CSP is unchanged:** `sandbox allow-scripts allow-downloads; … frame-ancestors 'none'`. No route loosens `frame-ancestors`. The report preview's `frame-ancestors 'self'` (`artifact_report_api._text`) becomes `'none'`, because nothing frames it any more.
9. **Lab links land directly on the live view for every type, with a Live↔Annotate switch** (operator, 2026-10-30; this replaces the earlier wrapper-page proposal).
   - **HTML.** The lab link lands on `f/<entry>`. `share-public` injects one mode-switch link into that response and nothing else:
     - **The link:** `<a data-herdr-mode-switch href="<"../" × depth>a/<path>" rel="noopener" style="position:fixed;right:12px;bottom:12px;z-index:2147483647;…">Annotate</a>`, with no script, no frame and no event handler.
     - **Where it goes:** before the last case-insensitive `</body>`, found by a bounded byte search over the file, or appended at the end when there is no `</body>`. The parser places trailing content into the body either way.
     - **When it is injected:** only when all of these hold:
       - the share is a lab share and annotations are enabled
       - the session is valid v2
       - the content type is `text/html`
       - the request carries `Sec-Fetch-Dest: document`
     - **Effect on other requests:** content `fetch`/XHR requests and sub-resources get the original bytes. A browser that sends no `Sec-Fetch-Dest` gets the original bytes and no switch.
     - **Headers on the modified response:**
       - `ETag: "<sha256>-m1"` and a recomputed `Content-Length`
       - `Vary: Sec-Fetch-Dest`
       - no `Accept-Ranges`; a `Range` request gets the full 200
       - the CSP is unchanged
     - **Navigation from the sandbox:** a CSP-sandboxed top-level document without `allow-top-navigation` may still navigate *itself* when the user clicks a plain anchor. Navigating its own browsing context is always allowed, and `allow-top-navigation` only governs navigating a *different* top-level context. So the sandbox needs no change. Task 11 pins this with a Playwright click from `f/` to `a/`.
   - **Other kinds.** The lab link lands on `v/<entry>`, our own page, whose mode bar carries the switch.
   - **Back from Annotate.** The annotation view's `Live` link goes to `f/<path>` for HTML and to `v/<path>` otherwise.
   - **Tradeoff, accepted:** content CSS (`* {display:none}`, overlays) or a content script can hide, cover, remove or alter the injected link. That only removes a convenience: the link carries no secret, and the content could already navigate anywhere. The `a/` URL itself always works.
   - **Public links are unchanged:** they land on the raw `f/<entry>`, with no injection and no switch.
   - The `#annotate` fragment and the separate "annotation link" are dropped.
10. **Established libraries, not hand-rolled code.**
    - **HTML sanitizer: `nh3`**, the Python binding of Rust's ammonia, which parses with html5ever, a spec-compliant HTML5 parser, so it resists mXSS. It goes into core `[project].dependencies` in `pyproject.toml` with a minimum of `nh3>=0.2.21`, following the repo's `>=` floor style.
      - Neither `nh3` nor `bleach` is in the repo's deps today.
      - `bleach` is deprecated upstream (January 2023) and its html5lib parser is unmaintained, so it is not used.
      - nh3 ships prebuilt abi3 manylinux wheels, which matters because CI hosts have no apt or compiler.
      - Core deps are the only set that reaches both the image (`deploy/fabric-stack/Dockerfile`: `pip install ".[collab,review-gate]"`), where `share-public` runs, and CI (`scripts/ci/unit-gates.sh`: `pip install -e '.[dev,collab,review-gate,federation]'`).
    - **PDF: pdf.js**, `pdfjs-dist@4.10.38`. Its prebuilt `build/pdf.min.mjs` and `build/pdf.worker.min.mjs` are vendored without a build step.
      - The vendoring follows the repo's convention for runtime JS (`tests/share-fixtures/_build/build-vendor.sh`): there are no runtime npm deps, and a script records a pinned version, runs `--ignore-scripts`, checks the npm `dist.integrity`, copies the LICENSE and records sha256.
      - A `THIRD_PARTY_NOTICES.md` entry is added.
      - At implementation time, re-check pdf.js security advisories. If a newer release that is at least two weeks old fixes one, pin that release instead and record it here.
    - **No client-side sanitizer (DOMPurify is not added).** Both consumers, the share views and the dashboard, receive markup that the server already cleaned. A second sanitizer in the browser would add a vendored library but no new property. The CSPs remain the backstop.
11. **PDF rendering with pdf.js is approved** (earlier operator question 4).
12. **`annotation_resolve` goes through plan 3's intent-kind registry**: `register_intent_kind` plus `CollabIntentApplier.register`. `Intent.kind` is not widened to a `Literal`.

## Consumed from plan 3 (`docs/superpowers/plans/2026-10-30-artifact-collaborators-mcp-bridge.md`)

Task 15 builds on these plan-3 names. If plan 3 changes them before it merges, follow plan 3.

- **Bridge files** (`collab_bridge_files.py`):
  - `read_json_beneath`, `atomic_write_json`, `new_ticket`, `TICKET_RE`, `BridgeLimits`, `Actor(kind, id)`.
  - `register_intent_kind(kind, validate_payload, *, actors)`.
  - `validate_intent(raw, *, filename, limits) -> Intent(ticket, kind, actor, artifact_id, idempotency_key, submitted_at, payload)`.
  - An unregistered kind gives `IntentError("unknown intent kind")`. `share_create` stays rejected.
- **Mirror** (`collab_mirror.py`, `CollabMirror`):
  - The worker owns the mirror, and it is read-only in `collab-public`.
  - The layout is `grants.json`, `artifacts/<id>/head.json`, `blobs/`, `results/<ticket>.json`.
  - `sync()` removes the directory of an artifact that is ungranted or retracted.
- **Applier** (`collab_intents.py`, `CollabIntentApplier`):
  - `register(kind, handler, *, lane_key=None, replay_outcomes)`.
  - `handler(ctx: IntentContext, intent) -> dict`, where `IntentContext` holds `actor, agent, conn, store, grants, limits, assemble, extras`.
  - The generic steps are the ledger replay, validation, `decided_for_key` replay, the 60 s age check and the `agents.json` freshness check for agent actors. The grant check (`grant_for`) is **not** generic: each handler runs it.
  - The result file is `results/<ticket>.json = {ticket, kind, actor: {kind, id}, artifact_id, result, reason?, decided_at, …handler fields}`.
- **collab-public** (`collab_artifacts.py`, `CollabArtifactBridge`):
  - `has_grants(pr)` and `has_write_grant(pr)`.
  - `append_intent(actor, kind, payload, *, artifact_id, idempotency_key) -> ticket`.
  - `read_result(actor, ticket, *, wait, still_valid)` and `result(pr, ticket, *, wait)`.
  - The pending-per-agent and outbox caps.
  - Denials go through `service._audit_denied(pr, "artifact.<tool>", reason, resource_id=...)`. Allowed reads are audited as `artifact_tool_used`.
- **MCP** (`collab_mcp.py`): `_ARTIFACT_TOOLS`, `ARTIFACT_TOOL_NAMES`, `tools_for` (offers the tools only when `has_grants`), `_call`, `artifact_commit_result`. Throttling uses `CollabService._throttle(key, limit, window, what)`.
- **Worker wiring:** `_cmd_cp_worker` builds the applier and registers `commit`. Plan 4 adds its registration there.

## Global Constraints

- **No frames.**
  - No HTML, template or JS we serve contains a frame or plugin tag or `srcdoc`, and nothing calls `createElement` for those elements or uses cross-document `postMessage`.
  - The sanitizer removes those elements from content.
  - Pinned by `tests/control_plane/test_no_frames.py` (Task 16) across `packages/`, `share_static/` (excluding `vendor/`), the Python sources that emit HTML, and the server-rendered responses.
  - The match is tag-aware, `<\s*(iframe|frame|frameset|object|embed|portal|fencedframe)(?=[\s/>])`, case-insensitive. It does not flag `length<Object.keys` (`packages/topology-view.js:147`) or the detector byte strings `b"<iframe"` in `intel_attachments.py`.
  - Hostile-input test strings (`*.test.js`, `tests/`) are not scanned.
- **Who may annotate.**
  - Only a lab-share viewer with a v2 session, minted from verified Cognito tokens that passed `share_lab.authorise`.
  - The owner annotates the same way, through the lab link.
  - Public shares and anonymous visitors have no `v/`, `a/` or annotations route: every such path is the uniform 404.
- **Text is never markup.**
  - Annotation text is plain text everywhere: `textContent` in `annotate.js`, `esc()` in the dashboard, and JSON data with the `UNTRUSTED` notice (`untrusted=True`) in MCP.
  - It is never written with `innerHTML`, never interpolated into HTML by the server, and never logged.
- **The only bytes we add to content** are the fixed mode-switch link of Decision 9, built from a constant template with the path `html.escape`d. It is pinned by a test.
- **Sanitized markup is only ever nh3 output.** The only `innerHTML` writes of content markup are:
  - the dashboard's `shadowRoot.innerHTML = html` of an owner render-route response
  - the server's `{{CONTENT}}` substitution of `share_render` output
- **Anchor kinds.** They are validated identically in `share-public` and in the worker by one pure module.
  - `{"kind": "document", "file"}` (any file type)
  - `{"kind": "text_quote", "file", "exact", "prefix", "suffix"}` (HTML and text renders)
  - `{"kind": "pdf_page", "file", "page", "exact"?, "region"?: {"x", "y", "w", "h"}}` (PDF)
  - `{"kind": "image_region", "file", "x", "y", "w", "h"}` (images, including SVG shown through `<img>`)
  - Parquet data views are not annotatable.
- **Caps.** One constant each in `annotation_anchor.py`:
  - request body ≤ 32 KiB
  - comment 1–`max_comment_chars` characters after strip, with a hard ceiling of `MAX_COMMENT_CHARS = 4000`; no C0 controls except `\n` and `\t`, no NUL and no surrogates
  - `exact` 1–1000 characters
  - `prefix` and `suffix` ≤ 64 characters each
  - `file` normalised by `share_bundle.normalise_name` and ≤ 1024 bytes
  - `page` 1–100000
  - region values are finite floats in [0, 1] with `x + w ≤ 1` and `y + h ≤ 1`
- **Rate limits** (Decision 6).
  - `share-public`, with the in-memory `RateLimiter`:
    - `per_user_per_minute` per (share, identity)
    - `per_share_per_minute` per share
    - `global_per_minute` overall
    - creates, edits and deletes all count
  - Durable caps, enforced at worker ingest so they survive `share-public` restarts:
    - `per_user_per_day` creates per (share, author) per day
    - `max_per_share` per share; withdrawn rows count
  - The edge allows 5 r/s per address with a burst of 30 on the annotations API.
  - Every refusal is a `denied` share event with a fixed reason code.
- **Annotations file.** `<events_dir>/annotations.jsonl` follows the same rules as `events.jsonl`, with its own constants:
  - one `os.write` per line under `O_APPEND`
  - each line ≤ 64 KiB
  - a file cap of `ANNOTATIONS_MAX_BYTES = 32 MiB`. When the file is full, the append is refused with 503 and the viewer keeps their text.
  - rotation past 4 MiB
  - idempotent ingest keyed by `sha256(line):offset`
- **Version pinning.**
  - An annotation stores `artifact_id` and `version_id`, both taken from the `artifact_shares` row at ingest, plus its anchor.
  - A new revision never changes a live share link. Addressing an annotation only records `addressed_in_version_id`.
- **Response headers for our views (`v/` and `a/`).**
  - `Content-Security-Policy: default-src 'none'; script-src 'nonce-<n>' 'strict-dynamic'; style-src-elem <origin>/s/static/ <origin>/s/<token>/ 'unsafe-inline'; style-src-attr 'unsafe-inline'; img-src <origin>/s/<token>/ data: blob:; font-src <origin>/s/<token>/ <origin>/s/static/ data:; connect-src <origin>/s/<token>/; worker-src <origin>/s/static/vendor/pdfjs/; frame-src 'none'; child-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
    - `<origin>` is the share origin, and `<n>` is `secrets.token_urlsafe(18)`, fresh per response.
    - `script-src` has no `'self'`, because bundle `.js` files are same-origin.
  - Also sent: `X-Frame-Options: DENY`, `Cache-Control: no-store`, `nosniff`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`.
  - **No CORS headers.** Our views, the annotations JSON and every annotations POST response are sent with `cors=False`. `serve_bundle_file` defaults to `cors=True` (`ACAO: *`), and the raw file's scripts hold the session path, so these responses must not be readable cross-origin.
- **Owner live route** (`GET /api/v1/artifacts/{id}/live`, Task 13):
  - `Content-Security-Policy: sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. A PDF gets `frame-ancestors 'none'` only, as `share_public.PDF_CSP` does.
  - Also sent: `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store`, and no ACAO.
  - The existing `/download` gains `Content-Disposition: attachment` and `Content-Security-Policy: default-src 'none'; sandbox`, so no artifact byte ever executes on the dashboard origin.
- **Configuration is YAML only** (no environment variables):
  - `control_plane.share.public_url`: an https origin with an empty path. It is required when `share.enabled` and must not equal `control_plane.collab.public_url`. Added to `SHARE_KEYS` and `SHARE_SLICE_KEYS`.
  - `control_plane.share.annotations`: `enabled` (bool, default `true`) plus the Decision 6 limits. Added to `SHARE_KEYS` and `SHARE_SLICE_KEYS`.
  - `control_plane.share.owner_emails`: a list, default `[]`. Added to `SHARE_KEYS` only, NOT `SHARE_SLICE_KEYS`.
  - The edge certificate directory follows the compose pattern `control_plane.compose.HERDR_SHARE_CERT_DIR`.
- **Migration.** Number `0045`: 0043 is taken by `feat/artifact-collab-gui`, 0044 is plan 3's and 0046 is plan 5's. Re-check `origin/main` and open branches before committing, and take the next free number. The migration must be additive and idempotent, with a `DOWN:` comment like 0043.
- **Never logged, and never stored outside the response that needs them:** tokens, sessions, CSRF tokens, nonces, `Authorization` and `X-Id-Token` values. Author emails reach only the owner API and the author's own projection, never the MCP mirror or an agent.
- **Code style.**
  - Python: `from __future__ import annotations`, type hints, and the surrounding module's style.
  - JS: ES modules (`share.js` stays a classic script, as today), 2-space indent, and the conventions of `artifact-share-panel.js`: `esc()`, `hd-*` classes, `data-*` test hooks, no inline handlers.
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Hostile bundle content cannot annotate as the viewer.**
  - In the raw file, a content script that knows the session path and tries a `fetch` POST to `annotations` gets the 404, and no line is written. This holds whether it sends no custom header (so no CSRF token), `Origin: null`, or triggers an `OPTIONS` preflight.
  - Its attempt to `fetch` `a/` and read the CSRF meta fails, because the response carries no CORS headers.
  - In our views, none of these payloads in the content ever runs:
    - `<script>`, `onerror=`, a `javascript:` link
    - `<meta http-equiv=refresh>`, `<base>`, `<form>`
    - `<svg><script>`, `<math>`, a `<noscript>` mXSS payload
  - Pinned in Tasks 7, 9 and 11.
- **Hostile annotation text** never executes. This covers `<img src=x onerror=…>`, `"><script>`, the RTL override `‮`, zero-width characters, and 4000 characters of one unbroken word.
  - In the annotation panel, the Feedback panel and the event drawer, it appears literally and wraps.
  - In MCP, it arrives inside the `UNTRUSTED` wrapper.
  - Pinned in Tasks 11, 14, 15 and 16.
- **The injected mode-switch link.**
  - It appears only on a lab `f/` document navigation of an HTML file, exactly once, and it is script-free.
  - Its `href` is escaped, and it carries no CSRF token or other secret.
  - Every other `f/` response is byte-identical to `main`: `fetch`/sub-resource requests, public shares, other kinds, and annotations disabled.
  - Content hiding or rewriting the link is the accepted tradeoff (Decision 9).
  - Pinned in Tasks 9 and 11.
- **Hostile artifacts on the owner dashboard.**
  - An HTML artifact or legacy output full of scripts, frames and `position: fixed` overlays renders statically in a shadow root, with no frame, no script effect and no hash-route hijack.
  - Its "Open live view" tab runs in an opaque origin and cannot read dashboard data.
  - Pinned in Tasks 13 and 16.
- **A session or share that dies mid-use.**
  - Revoke, expiry or session expiry between page load and a submit or edit gives the POST the uniform 404. The panel shows "Your session ended — open the link again" and keeps the typed comment.
  - A line written before the revoke is still ingested when its `at` precedes `revoked_at`; a line after it is refused.
  - Pinned in Tasks 4, 9 and 11.
- **Forged or replayed lines from a compromised or restarted `share-public`.**
  - The cases are an unknown share, a public share, a file not in the pinned version, quota exceeded, a duplicate `annotation_id`, and an `artifact_id`/`version_id` field in the line, which is ignored.
  - So are an edit or delete by another identity, and an edit or delete of a row that is not open.
  - Each case is either refused with a `denied` event or applied exactly once.
  - Pinned in Tasks 3 and 4.
- **Content that defeats the static render.**
  - Each of these still shows the mode bar and the panel, and still allows a whole-file comment:
    - an HTML entry over the render cap
    - a page whose CSS sets `* { display: none }`
    - `position: fixed` overlays
    - a page with only script-built content
  - A malformed PDF shows "This PDF could not be drawn" and still allows a whole-file comment.
  - Pinned in Tasks 7 and 11.

---

### Task 1: Anchor and comment validation (pure)

**Files:** Create `herdr_engineering/control_plane/annotation_anchor.py`; Test `tests/control_plane/test_annotation_anchor.py`

**Interfaces:**
- **Constants:** `MAX_BODY_BYTES = 32768`, `MAX_COMMENT_CHARS = 4000`, `MAX_EXACT_CHARS = 1000`, `MAX_CONTEXT_CHARS = 64`, `MAX_PAGE = 100000`, `ANCHOR_KINDS = ("document", "text_quote", "pdf_page", "image_region")`, `ANNOTATION_ID_RE = re.compile(r"ann_[A-Za-z0-9_-]{22}")`.
- **Error:** `class AnnotationInputError(ValueError)` with `.code` in `bad_anchor | bad_file | empty | too_long | bad_chars | bad_id`.
- **Functions:**
  - `def clean_comment(value: object, *, max_chars: int = MAX_COMMENT_CHARS) -> str`
  - `def clean_anchor(value: object) -> dict` returns a new dict holding only the allowed keys for its kind, with `file` normalised. A `pdf_page` `region` is validated like `image_region`'s fields.
  - `def parse_annotation_request(raw: bytes, *, max_chars: int = MAX_COMMENT_CHARS) -> tuple[dict, str]` parses `{"anchor": {...}, "comment": "..."}`. Unknown top-level keys → `bad_anchor`.
  - `def parse_edit_request(raw: bytes, *, max_chars: int = MAX_COMMENT_CHARS) -> str` accepts `{"comment"}` only.
  - `def parse_delete_request(raw: bytes) -> None` accepts only `{}`.
- Nothing raises anything but `AnnotationInputError`.

- [ ] **Step 1: Write failing tests**
  - Each kind is accepted with its minimal and with its maximal fields. This includes `pdf_page` with and without `region`.
  - Extra keys are stripped inside the anchor but refused at the top level.
  - Boundaries are exact:
    - comment: 4000 accepted, 4001 → `too_long`; with `max_chars=500`, 501 → `too_long`
    - `exact`: 1000 accepted, 1001 refused
    - prefix: 64 accepted, 65 refused
    - page: 0, 1, 100000 and 100001
    - `True` as a page is refused
    - a region (both kinds) with `x + w = 1.0000001` is refused, and so are NaN and inf
  - The comment `"  \n "` → `empty`. `"\x00"`, `"\x1b"` and `"\ud800"` → `bad_chars`. `"\n"` and `"\t"` are kept.
  - `file` values `"../a"`, `"a//b"`, `"/abs"` and a 1025-byte name → `bad_file`.
  - Non-JSON, a JSON list and 32769 bytes → `bad_anchor` / `too_long`.
  - A body of 4000 `"😀"` plus a 1000-character `exact` is accepted.
  - `parse_edit_request(b'{"comment":"x","anchor":{}}')` → `bad_anchor`; `parse_delete_request(b'{"x":1}')` → `bad_anchor`.
  - A hostile comment `<img src=x onerror=1>` is returned unchanged: it is stored as text, not filtered.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_annotation_anchor.py -q` → FAIL (the module is missing).
- [ ] **Step 3: Implement.** Reuse `share_bundle.normalise_name` for `file`, and parse with `json.loads` on the bytes after a length check.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(annotations): anchor and comment validation`.

### Task 2: Reusable JSONL ingestor (refactor, behaviour pinned)

**Files:** Create `herdr_engineering/control_plane/jsonl_ingest.py`; Modify `herdr_engineering/control_plane/artifact_shares.py` (`ShareStore.ingest_events_file` / `ingest_and_rotate_events` delegate to it); Test the existing `tests/control_plane/test_share_store.py` and `test_share_worker.py`, plus a new `tests/control_plane/test_jsonl_ingest.py`

**Interfaces:**
- `class JsonlIngestor` with `__init__(self, conn, *, parse_line: Callable[[bytes], Any | None], store: Callable[[Any, Any, str], Awaitable[bool]], max_line_bytes: int = 16384)`.
  - `store(conn, parsed, ingest_key)` runs inside a per-line savepoint and returns True when a row was inserted.
  - Annotations pass `max_line_bytes=65536`.
- `async def ingest_file(self, path: Path, marker: Path) -> int`
- `async def ingest_and_rotate(self, path: Path, marker: Path, *, rotate_bytes: int, now: float, grace: float) -> int`
- These move here unchanged from `ShareStore`: the offset/fingerprint marker, the torn-tail rule, oversize-line skipping, the rotation to `.1`, the regular-file-only open and the poison-line skip.

- [ ] **Step 1: Write failing tests** in `test_jsonl_ingest.py`, with an in-memory `store` callback (no Postgres):
  - A torn last line waits for its newline.
  - A replay after the marker is deleted calls `store` with the same `ingest_key` values.
  - A symlink planted at `path` is ignored.
  - A `store` that raises `ValueError` skips and consumes that line.
  - A 40 KiB line is skipped at the default limit and passed through with `max_line_bytes=65536`.
  - Rotation happens only once `rotate_bytes` are consumed. The `.1` file is drained and deleted on the next call after `grace`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** by moving the code. `ShareStore` keeps its public method names and `_parse_line`. It passes an adapter closure `store(conn, parsed, key)` that unpacks the parsed tuple into `self._event(conn, share_id, kind, actor, detail, at, key)`.
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_jsonl_ingest.py tests/control_plane/test_share_store.py tests/control_plane/test_share_worker.py -q` → PASS, with no edits to the existing share tests.
- [ ] **Step 5: Commit** `refactor(shares): extract the JSONL ingestor`.

### Task 3: Migration 0045 and `AnnotationStore`

**Files:** Create `deploy/control-plane/migrations/0045_artifact_annotations.sql` and `herdr_engineering/control_plane/artifact_annotations.py`; Test `tests/control_plane/test_artifact_annotations_store.py` (Postgres fixtures as in `test_share_store.py`)

**Interfaces:**
- **Migration** (additive and idempotent):
  - Table `artifact_annotations`, with these columns:
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
    - `resolved_by text`, `resolved_at timestamptz`
    - `edited_at timestamptz`, `withdrawn_at timestamptz`
    - `created_at timestamptz NOT NULL`
    - `ingested_at timestamptz NOT NULL DEFAULT now()`
    - `updated_at timestamptz NOT NULL DEFAULT now()`
    - `ingest_key text UNIQUE`
  - Table CHECK `(status = 'addressed') = (addressed_in_version_id IS NOT NULL)`.
  - Indexes `(artifact_id, status, created_at DESC, annotation_id)`, `(share_id, author_identity, created_at DESC)` and `(updated_at)`.
  - Table `artifact_annotation_ops(ingest_key text PRIMARY KEY, annotation_id text NOT NULL, op text NOT NULL CHECK (op IN ('edit','delete')), at timestamptz NOT NULL)`, the idempotency ledger for edit and delete lines.
  - The `artifact_share_events.kind` CHECK is widened to add `'annotated'`, `'annotation_edited'`, `'annotation_withdrawn'`, `'annotation_dismissed'` and `'annotation_addressed'`. The constraint is dropped and re-added by name inside a `DO` block that is a no-op once widened.
- **Store** `AnnotationStore(conn)`:
  - `async def ingest(self, conn, record: dict, ingest_key: str, *, owner_emails: frozenset[str], per_user_per_day: int, max_per_share: int, max_comment_chars: int) -> str` dispatches on `record.get("op", "create")`.
    - **`create`** returns `"inserted"`, `"duplicate"` or a refusal: `unknown_share`, `not_lab`, `not_live`, `file_not_in_version`, `quota_user_day`, `quota_share` or `invalid`.
      - `artifact_id` and `version_id` come from the share row. Fields of that name in `record` are ignored.
      - `file` must be one of the names the share exposes, from `shared_file_names(artifact_row, version_row) -> frozenset[str]`. This new helper in `artifact_share_api.py` is factored out of `_plan`, which then uses it.
      - The share must have been live at `record["at"]`, and `at` is clamped to at most now + 5 min.
      - `author_role` is `owner` when the email is in `owner_emails`.
    - **`edit` / `delete`** return `"edited"` / `"withdrawn"`, `"duplicate"`, or a refusal: `not_found`, `not_author`, `not_open`, `not_live` or `invalid`.
    - A refusal writes a `denied` share event `{"reason": "annotation_" + code}`. A success writes `annotated` / `annotation_edited` / `annotation_withdrawn` with `{"annotation_id", "file"}`.
  - `async def list_for_artifact(self, artifact_id: str, *, status: str = "open", limit: int = 200, before: str | None = None) -> tuple[list[dict], str | None]`.
    - Rows carry `version_number`, `addressed_in_version_number` and `edited_at`.
    - `status` is one of `open | addressed | dismissed | withdrawn | all`, and `limit` is 1–500.
    - The cursor is `"<created_at iso>|<annotation_id>"`.
  - `async def dismiss(self, artifact_id: str, annotation_id: str, *, actor: str) -> dict` raises `AnnotationError(code="not_found" | "not_open")`. Dismissing a row that is already dismissed returns it unchanged.
  - `async def resolve(self, artifact_id: str, annotation_ids: list[str], *, version_id: str, actor: str) -> dict` returns `{"resolved": [ids], "skipped": [{"annotation_id", "reason"}]}`.
    - Skip reasons are `not_found`, `not_open` (which includes `withdrawn`) and `version_not_newer`.
    - A `version_id` that belongs to another artifact raises `AnnotationError("bad_version")`.
    - It sets `updated_at` and writes one `annotation_addressed` event per resolved row.
  - `async def own_rows(self, share_id: str) -> dict[str, list[dict]]` maps author_identity to that author's newest 500 rows, withdrawn rows excluded. Each row has `annotation_id, file, anchor, body, status, version_number, addressed_in_version_number, created_at, edited_at`.
  - `async def open_for_agents(self, artifact_id: str, limit: int = 200) -> list[dict]` returns rows with `annotation_id, version_id, version_number, file, anchor, body, author_role, created_at, edited_at`, and no email or identity.
  - `async def touched_since(self, since: datetime) -> tuple[set[str], set[str]]` returns the share ids and the artifact ids.

- [ ] **Step 1: Write failing tests**
  - Ingest:
    - The migration runs twice cleanly.
    - A valid create inserts one row with the share's artifact and version, even when the line carries a different `artifact_id`.
    - The same `ingest_key` gives `duplicate`.
    - Each create refusal code is produced by its input and leaves a `denied` event.
    - A `preview.html` annotation on a report share is ingested. `index.html` on that report share → `file_not_in_version`.
    - The 2001st create of one author in one day → `quota_user_day`. With `max_per_share=3`, the 4th create on a share → `quota_share`.
    - A line timed after `revoked_at` → `not_live`. A line timed before it → inserted.
    - An owner email → `author_role = 'owner'`.
  - Edit and delete:
    - An edit by the author → `edited`, with one event.
    - An edit by another identity → `not_author`.
    - An edit of a dismissed row → `not_open`.
    - Replaying an edit → `duplicate`.
    - A delete → `withdrawn`. The row leaves `own_rows`, `open_for_agents` and the `open` list, and stays under `withdrawn`.
  - Owner and agent operations:
    - `dismiss` of an addressed row → `not_open`.
    - `resolve`: an older version → `version_not_newer`; another artifact's version → `bad_version`; a withdrawn row → `not_open`; `updated_at` advances.
    - Paging: 3 rows with `limit=2` → 2 rows plus a cursor, then 1 row with no cursor.
    - `open_for_agents` rows have no email or identity.
    - A hostile body round-trips byte-for-byte.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_annotations_store.py -q` → FAIL.
- [ ] **Step 3: Implement.** Validate `record["anchor"]`/`record["body"]` with Task 1; an error gives `invalid`.
- [ ] **Step 4: Run** the new tests plus `test_share_store.py` → PASS.
- [ ] **Step 5: Commit** `feat(annotations): table, store, edit/withdraw and audit events`.

### Task 4: Worker ingest, viewer projections, YAML settings, owner email on lab shares

**Files:**
- Modify:
  - `herdr_engineering/control_plane/worker.py`: a new `AnnotationHousekeeper`, next to `ShareHousekeeper`
  - `herdr_engineering/cli.py`: construct the housekeeper near line 4007
  - `herdr_engineering/fleet_config_cli.py`: `SHARE_KEYS` gains `"annotations"` and `"owner_emails"`, and `SHARE_SLICE_KEYS` gains `"annotations"`
  - `herdr_engineering/control_plane/artifact_share_api.py`: `share_settings_config` reads both keys; `_create` appends `owner_emails` to a lab share's `allow_emails`
  - `herdr_engineering/control_plane/share_bundle.py`: the projection writer
- Test: `tests/control_plane/test_annotation_worker.py`, and extend `tests/control_plane/test_artifact_share_api.py` and the fleet-config tests

**Interfaces:**
- `ANNOTATION_DEFAULTS = {"enabled": True, "per_user_per_minute": 60, "per_share_per_minute": 600, "global_per_minute": 3000, "per_user_per_day": 2000, "max_per_share": 20000, "max_comment_chars": 4000}`, defined in `annotation_anchor.py`.
- `def annotation_settings(raw: dict | None) -> dict` merges `raw` over the defaults. Loading refuses with `FleetConfigError`:
  - unknown keys
  - a non-bool `enabled`
  - a non-int or ≤ 0 limit
  - a `max_comment_chars` outside 1–4000
- `def write_own_projection(root: Path, share_id: str, identity: str, rows: list[dict]) -> None` writes `<root>/<share_id>/annotations/<sha256(identity)>.json`.
  - The write is atomic, mode 0644.
  - It is capped at 2 MiB: newest rows first, and a cut file carries `"truncated": true`.
  - It is skipped when `<root>/<share_id>` is missing, and it never follows a symlink.
- `class AnnotationHousekeeper(shares_root, events_dir, marker_dir, settings, *, owner_emails, mirror=None, interval=15.0, clock=time.time)` with `async tick(conn) -> dict | str`. Each tick:
  1. ingests and rotates `annotations.jsonl` (marker `annotation.marker.json`, `max_line_bytes=65536`);
  2. rewrites the projections of the touched shares;
  3. if `mirror` is set, writes `annotations.json` for the touched artifacts (Task 15);
  4. on the first tick after start, treats everything as touched.

  It never raises out of the worker pass.

- [ ] **Step 1: Write failing tests**
  - Projections:
    - A create line becomes a row and appears in its author's projection only.
    - Edit and delete lines are reflected on the next tick.
    - A dismiss is reflected on the next tick.
    - A revoked share's directory is not recreated.
  - Settings:
    - The `annotation_settings` defaults equal `ANNOTATION_DEFAULTS`.
    - `annotation_settings` refuses a non-bool `enabled`, `per_user_per_minute: 0`, `max_comment_chars: 4001` and an unknown key.
    - `owner_emails` with an invalid email → `FleetConfigError`.
    - `share_values` accepts both new keys. A rendered slice contains `annotations` but never `owner_emails`.
  - Lab shares:
    - A lab share created with `owner_emails: ["owner@lab.test"]` lists it in `allow_emails`, de-duplicated and lower-cased.
    - A public share is unchanged.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new and touched tests → PASS.
- [ ] **Step 5: Commit** `feat(annotations): worker ingest and viewer projections`.

### Task 5: Share host from YAML (`control_plane.share.public_url`)

**Files:**
- Modify:
  - `herdr_engineering/fleet_config_cli.py`: `SHARE_KEYS` and `SHARE_SLICE_KEYS` gain `"public_url"`
  - `herdr_engineering/cli.py`: every `share_settings_config` call site passes `public_url=share["public_url"]`; `_share_public_server` sets `ShareConfig.public_url`; `_share_lab_problems` names the new key
  - `herdr_engineering/control_plane/artifact_share_api.py`: the error text
- Test: the fleet-config tests, `tests/control_plane/test_artifact_share_api.py` and `tests/control_plane/test_share_lab.py`

**Interfaces:**
- When `share.enabled` is true, `share_values(cfg)` requires `control_plane.share.public_url` to be:
  - an https origin with path `""` or `/`
  - without a query or fragment
  - on a netloc different from `control_plane.collab.public_url`'s

  Otherwise it raises `FleetConfigError` naming the key: "share links and the lab sign-in callback /s/callback live at the root of the share host; MCP stays on control_plane.collab.public_url".
- `ShareConfig.public_url` is the share host. It is the source of:
  - `lab_settings`, with redirect_uri `<share>/s/callback`
  - `ShareServer.public_origin`, which feeds the sandbox CSP and our views' CSP
  - the link base in `artifact_share_api`
- `SHARE_COLLAB_KEYS` is unchanged.

- [ ] **Step 1: Write failing tests**
  - Refused:
    - A missing `share.public_url` → `FleetConfigError` naming `control_plane.share.public_url`.
    - A value equal to the collab URL.
    - A path, a query, or `http://`.
  - Accepted:
    - The slice's `share.public_url` is the share host.
    - An approved share's URL starts with `https://artifacts.example.test/s/`.
    - `redirect_uri` is `https://artifacts.example.test/s/callback`.
    - `sandbox_csp` names the share origin.
  - A disabled share needs nothing new.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_artifact_share_api.py tests/control_plane/test_share_lab.py tests/test_share_deploy_config.py -q` plus the fleet-config tests → PASS.
- [ ] **Step 5: Commit** `feat(share): serve share links from their own host (control_plane.share.public_url)`.

### Task 6: v2-only sessions (bound to the verified email), the CSRF token, and the live landing

**Files:**
- Modify:
  - `herdr_engineering/control_plane/share_session.py`
  - `herdr_engineering/control_plane/share_public.py`: `_session` mints v2 and returns `landing`; `_SESSION_SEGMENT` max 768
  - `herdr_engineering/control_plane/share_static/share.js`: `BASE_PATH` `{1,768}`; navigates to `landing`
- Test: extend `tests/control_plane/test_share_lab.py` and `tests/control_plane/test_share_public.py`

**Interfaces:**
- `mint_session(secret, share_id, identity, exp, *, email: str) -> str`.
  - `email` is required.
  - The format is `<b64 id>.<b64 email>.<exp>.<b64 mac>`, with domain tag `herdr-share-session-v2`. The MAC covers the length-prefixed email.
  - The v1 format and `_DOMAIN = b"herdr-share-session-v1"` are **deleted**.
- `check_session(...) -> {"identity", "exp", "email"} | None` accepts v2 only.
- `MAX_SESSION = 768`.
- `def csrf_token(secret: bytes, share_id: str, session: str) -> str` is b64url(HMAC-SHA256(secret, b"herdr-annotate-csrf-v1\x00" + len-prefixed share_id + len-prefixed session)), 43 characters.
- `def check_csrf(secret, share_id, session, value) -> bool` compares with `hmac.compare_digest`.
- The session response is `{"base": ".../k/<session>/f/", "entry", "landing", "expires_at"}`. `landing` is `.../k/<session>/f/<entry>` when `content_type_for(entry)` is `text/html`, else `.../k/<session>/v/<entry>`.
- `share.js` replaces the location to `landing`. It has no hash handling, and no other behaviour changes.

- [ ] **Step 1: Write failing tests**
  - Round trip:
    - A v2 round trip returns the email.
    - Changing one byte of the email part → None.
    - A v1-format session (built by hand in the test) → None.
    - A v2 session for another share → None.
    - The session length is ≤ 768 for a 128-character identity and a 254-character email.
  - CSRF:
    - `csrf_token` is stable for one session and differs across sessions and shares.
    - `check_csrf` refuses a one-character change.
  - The session response:
    - `_session` returns a v2 session.
    - For an HTML entry, `landing` ends in `/f/<entry>`. For a PDF or PNG entry, it ends in `/v/<entry>`.
  - `mint_session` without `email` is a `TypeError`.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_share_public.py tests/control_plane/test_share_lab.py -q` → FAIL.
- [ ] **Step 3: Implement.** `_session` passes `grant["email"]` to `mint_session`. Update the existing tests that minted v1 sessions to pass an email; do not keep v1 fixtures.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(share): email-bound v2 sessions, annotate CSRF token, live landing`.

### Task 7: Static render: an adapter around `nh3` (pure)

**Files:** Modify `pyproject.toml` (core `dependencies` gains `"nh3>=0.2.21"`); Create `herdr_engineering/control_plane/share_render.py`; Test `tests/control_plane/test_share_render.py`

**Interfaces:**
- **Constants:**
  - `RENDER_VERSION = 1`
  - `MAX_RENDER_INPUT = 2 * 1024 * 1024`
  - `MAX_RENDER_OUTPUT = 4 * 1024 * 1024`
  - `STATIC_NOTE = "Scripts, frames and embedded media are not shown in this static copy"`
- `def render_kind(name: str, media_type: str | None = None) -> str` uses `media_type` when it is given, else `content_type_for(name)`:
  - `"html"` for `text/html`
  - `"text"` for other `text/*` and for `application/json`
  - `"image"` for `image/*`
  - `"pdf"` for `application/pdf`
  - `"document"` for everything else
- `Resolver = Callable[[str, str], str | None]`. It is called as `resolve(context, url)`, where `context` is one of `"a"` (a link), `"img"`, `"link"` (a stylesheet) or `"css"` (a `url()` in a `style` attribute). It returns the rewritten URL, or None to drop the attribute.
- `def share_resolver(name: str, index: Collection[str]) -> Resolver` is the share host's rewriting for the annotation view. Only `a/` renders HTML, because HTML's live view is `f/`.
  - A relative URL is resolved against `name`'s directory and normalised by `normalise_name`. If it is in `index`, it becomes `<"../" × depth>a/<path>` for `a`, and `<"../" × depth>f/<path>` for `img`, `link` and `css`. A fragment is kept.
  - A fragment-only `#x` is kept.
  - For `a`, an absolute `http:`, `https:` or `mailto:` URL is kept.
  - For `img`, a `data:image/(png|jpeg|gif|webp);base64,` URL is kept.
  - Everything else is dropped: `javascript:`, `vbscript:`, other `data:` URLs, `//host`, and names outside `index`.
- `def sanitize_html(data: bytes, *, resolve: Resolver) -> str` returns a fragment.
  - It decodes the bytes with `errors="replace"`, then calls `nh3.clean` once:
    - `tags`: the element allow-list `a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup data dd del details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li link main mark nav ol p pre q rp rt ruby s samp section small span strong style sub summary sup table tbody td tfoot th thead time tr u ul var wbr`
    - `clean_content_tags`: `script template iframe frame frameset noframes object embed applet portal fencedframe svg math canvas video audio textarea select title noembed xmp plaintext`
    - `attributes`:
      - global: `class id title lang dir role style`
      - `a`: `href`
      - `img`: `src alt width height`
      - `td`/`th`: `colspan rowspan headers scope`
      - `ol`: `start reversed type`
      - `li`: `value`
      - `col`/`colgroup`: `span`
      - `time`/`data`: `datetime value`
      - `link`: `rel href`
    - `generic_attribute_prefixes={"aria-"}`
    - `url_schemes={"http", "https", "mailto", "data"}`
    - `link_rel="noopener noreferrer"`
    - `set_tag_attribute_values={"a": {"target": "_blank"}}`
    - `strip_comments=True`
    - `attribute_filter`: a function that routes `a[href]`, `img[src]` and `link[href]` through `resolve`. It keeps `link` only with `rel="stylesheet"`, and rewrites `url(...)` in `style` values through `resolve("css", …)` with one bounded, non-backtracking regex.
  - Everything the library does not allow is dropped. That includes `form`, `meta`, `base`, `noscript` and every `on*` attribute.
  - If the input is over `MAX_RENDER_INPUT` or the output over `MAX_RENDER_OUTPUT`, it raises `RenderTooLarge`.
- `def render_text(data: bytes) -> str` returns `<pre class="herdr-text">…html.escape…</pre>`, capped the same way.
- `def wrap_content(kind: str, inner: str) -> str` returns `<p class="herdr-static-note">STATIC_NOTE</p><div id="herdr-content" data-kind="<kind>"><template shadowrootmode="open">` + inner + `</template></div>`.
- `class RenderCache(max_entries=32, max_bytes=64 * 1024 * 1024)` is a thread-safe LRU keyed by `(sha256, name, RENDER_VERSION)`.

- [ ] **Step 0: Verify the library before writing the adapter** (a throwaway script in the scratch dir, not committed). Record the results in the module docstring.
  - `pip download nh3` gives an abi3 manylinux wheel that installs on Python 3.12 and 3.13 x86_64.
  - `nh3.clean('<style>a{}</style><p>x</p>', tags={'style', 'p'}, clean_content_tags={'script'})` keeps the `<style>` element.
    - **If it does not**, remove `style` from `tags`, add it to `clean_content_tags`, and widen the Decision 1 tradeoff to "author `<style>` blocks are not kept". Do not write a pre-pass.
  - `<link rel=stylesheet href=x>` survives with `link` in `tags`. **If it does not**, drop `link` the same way.
  - Find out whether `attribute_filter` runs before or after the URL-scheme check. The adapter must not depend on the order. Relative URLs it returns must survive, and so must the absolute URLs it keeps.
  - Overlapping `tags` and `clean_content_tags` raises. The adapter never passes an overlap.
- [ ] **Step 1: Write failing tests** (one assertion group per line)
  - None of these payloads leaves a `script` element, an `on*` attribute, a `javascript:` URL, `<base` or `<meta` in the output, checked by re-parsing it:
    - `<script>alert(1)</script>`
    - `<img src=x onerror=alert(1)>`
    - `<a href="javascript:alert(1)">`
    - `<a href=" JaVaScRiPt:alert(1)">`
    - `<meta http-equiv=refresh content="0;url=//evil">`
    - `<base href=//evil/>`
    - `<form action=//evil><input>`
    - `<svg><script>…</script></svg>`
    - `<math><mtext><table><mglyph><style><img src=x onerror=1>`
    - `<noscript><p title="</noscript><img src=x onerror=1>">`
    - `<style></style><img src=x onerror=1>`
  - `<iframe src=a.html>`, `<frame>`, `<frameset><frame>`, `<object data=x>`, `<embed src=x>` and `<svg><foreignObject><iframe>` give output that matches no frame or plugin tag (the Global Constraints regex).
  - Unterminated markup gives a balanced fragment, and `wrap_content` output ends with `</template></div>`.
  - Links through `share_resolver(...)`:
    - `href="page2.html"` from `index.html` → `a/page2.html`.
    - From `sub/x.html`, `../index.html` → `../a/index.html`.
    - `img/p.png` → `f/img/p.png`.
    - A name not in the index → no `href`.
    - `https://example.org` gets `rel="noopener noreferrer"` and `target="_blank"`.
    - A `data:image/svg+xml` src is dropped.
  - `style="color:red;background:url(img/p.png)"` → the URL is rewritten to `f/img/p.png`. `<style>a{}</style>` is kept, or dropped, per the Step 0 outcome.
  - A 2 MiB + 1 input → `RenderTooLarge`. 100 000 nested `<div>`s render within 5 s without raising.
  - `render_kind`: `report.html` → html, `data.csv` → text, `a.svg` → image, `a.pdf` → pdf, `t.parquet` → document.
  - `RenderCache`: a hit after put; eviction by count and by bytes.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_share_render.py -q` → FAIL.
- [ ] **Step 3: Implement** the adapter. No hand-written HTML tokenizer or serializer.
- [ ] **Step 4: Run** → PASS. Run `pip install -e '.[dev]'` in a fresh venv to confirm that `nh3` resolves.
- [ ] **Step 5: Commit** `feat(share): nh3-backed static render for the share and dashboard views`.

### Task 8: Vendor pdf.js once, served by both servers

**Files:**
- Create:
  - `scripts/vendor-pdfjs.sh`
  - `herdr_engineering/control_plane/share_static/vendor/pdfjs/{pdf.min.mjs,pdf.worker.min.mjs,LICENSE,SHA256SUMS}`
  - `tests/control_plane/test_pdfjs_vendor.py`
- Modify:
  - `pyproject.toml`: package-data gains `"control_plane/share_static/vendor/pdfjs/*"`, because the existing `*.js` globs miss `vendor/` and `.mjs`
  - `THIRD_PARTY_NOTICES.md`: a pdf.js (Apache-2.0) entry
  - `herdr_engineering/control_plane/api.py`: `_PUBLIC_STATIC` gains `/vendor/pdfjs/pdf.min.mjs` and `/vendor/pdfjs/pdf.worker.min.mjs` as `text/javascript`, read from the same package dir
  - `herdr_engineering/control_plane/share_public.py`: `STATIC_FILES` / `_viewer_asset` gain the one-level `vendor/pdfjs/<name>` allow-list, with `VENDOR_MAX_BYTES = 4 MiB`; `STATIC_MAX_BYTES` stays 1 MiB

**Interfaces:**
- `scripts/vendor-pdfjs.sh <empty scratch dir>` follows `tests/share-fixtures/_build/build-vendor.sh`.
  - It pins `PDFJS=4.10.38` and runs `npm pack pdfjs-dist@$PDFJS --ignore-scripts`.
  - It compares the tarball's sha512 with `npm view pdfjs-dist@$PDFJS dist.integrity`.
  - It copies only `build/pdf.min.mjs`, `build/pdf.worker.min.mjs` and `LICENSE`, and writes `SHA256SUMS`.
- Both servers serve the files with `nosniff` and a long `Cache-Control`.
  - The dashboard CSP's `script-src 'self'` covers both the module and the worker.
  - The share views' CSP names `worker-src <origin>/s/static/vendor/pdfjs/`.
- `annotate.css` and the dashboard preview CSS carry the `.textLayer` rules from `pdfjs-dist/web/pdf_viewer.css`, with attribution.

- [ ] **Step 1: Write failing tests**
  - Every file under `vendor/pdfjs/` matches `SHA256SUMS`.
  - `LICENSE` is Apache-2.0, and no other file is present.
  - Both files are listed by `importlib.resources` in an installed wheel (build it with `pip wheel . --no-deps` into a temp dir).
  - `GET /vendor/pdfjs/pdf.min.mjs` on the dashboard test server, and `/s/static/vendor/pdfjs/pdf.min.mjs` on share-public, return 200 with `text/javascript`.
  - `/s/static/vendor/pdfjs/../share.js` → 404.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Run the script, then implement** the static map entries.
- [ ] **Step 4: Run** the new tests plus `tests/control_plane/test_share_public.py` → PASS.
- [ ] **Step 5: Commit** `build(web): vendor pinned pdf.js for static PDF rendering`.

### Task 9: Live views (the HTML mode-switch link, `v/`), the annotation view and the annotation routes in `share-public`

**Files:** Modify `herdr_engineering/control_plane/share_public.py` (the module docstring's security rules: the three annotation POSTs are the only routes that read a body); Create `herdr_engineering/control_plane/share_static/view.html` (one template for both modes); Test extend `tests/control_plane/test_share_public.py`

**Interfaces:**
- **Config and server state.**
  - `ShareConfig` gains `annotations: dict` (from `annotation_settings`) and `annotations_path: Path`, which defaults to `events_path.with_name("annotations.jsonl")`.
  - `ShareServer` gains:
    - the `annotate_user`, `annotate_share` and `annotate_global` `RateLimiter`s
    - `AnnotationLog.append(record) -> bool`
    - `render_cache`
    - `annotate_origin: str | None`, which defaults to `public_origin`
- **The session check.** Every route below first checks the session against the current live lab state, else the uniform 404 plus a `denied` reason.
  - A v2 session and `lab is not None` are required, and a public share is always the 404.
  - The `a/` page and the annotations routes also require annotations to be enabled.
- **HTML live view: the mode-switch link on `f/`** (Decision 9).
  - `serve_bundle_file` gains a `mode_switch: str | None` argument, computed by `_route`.
  - `_route` sets it only when every one of these holds:
    - the share is a lab share
    - annotations are enabled
    - the session is valid v2
    - `content_type_for(path)` is `text/html`
    - the request's `Sec-Fetch-Dest` is exactly `document`
  - The value is `mode_switch_link(depth, path)`:
    - `MODE_SWITCH_TEMPLATE = '<a data-herdr-mode-switch href="{href}" rel="noopener" style="position:fixed;right:12px;bottom:12px;z-index:2147483647;display:block;margin:0;padding:6px 12px;font:600 14px/1.4 system-ui,sans-serif;color:#fff;background:#1f4fd1;border-radius:6px;text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,.4)">Annotate</a>'`
    - `href` = `html.escape('<"../" × depth>a/' + path)`
  - `def inject_mode_switch(data: bytes, link: bytes) -> bytes` inserts `link` before the last case-insensitive `</body>`, using `bytes.lower().rfind(b"</body")` over the already size-capped file, or appends it when there is none.
  - The modified response is the full 200:
    - `ETag: "<sha256>-m1"` and a recomputed `Content-Length`
    - `Vary: Sec-Fetch-Dest`
    - no `Accept-Ranges`; a `Range` header is ignored
    - `If-None-Match` is compared against the `-m1` tag
    - CSP, CORS and the other headers stay as for `f/` today
  - All other `f/` responses are unchanged.
- **`GET|HEAD /s/<token>/k/<session>/v/<path>` and `.../a/<path>`.**
  - `<path>` must be in the state index, else 404 `not_in_bundle`. `v/` of a path whose kind is `html` is 404 `not_in_mode`.
  - The response is `view.html` with these placeholders replaced by `str.replace`, each exactly once and the content last:
    - `{{NONCE}}`
    - `{{MODE}}` (`live` or `annotate`)
    - `{{CSRF_META}}`: the `<meta name="herdr-annotate-csrf">` element on `a/`, empty on `v/`
    - `{{SWITCH}}`: the mode bar, a `<nav data-mode-switch>` with two links. `Live` → `<"../" × depth>f/<path>` for an `html` kind, else `<"../" × depth>v/<path>`, and `Annotate` → `<"../" × depth>a/<path>`; the current mode is `aria-current="page"`. When annotations are disabled, the bar has `Live` only.
    - `{{DATA_JSON}}`: `<script type="application/json" id="herdr-view-data" nonce>` holding `{file, kind, mode, entry, expires_at, max_comment_chars, raw_href}`, where `raw_href` = `<"../" × depth>f/<path>`
    - `{{CONTENT}}`
  - `{{CONTENT}}` by kind:
    - `html`: `wrap_content("html", sanitize_html(..., resolve=share_resolver(path, index)))` from `render_cache` (`a/` only, since `v/` of HTML is the 404). Over the cap, the message is "This page is too large to show as a static copy — open the live page, or comment on the whole file", and the kind becomes `document`.
    - `text`: `wrap_content("text", render_text(...))`.
    - `image`, `pdf`, `document`: empty, because `annotate.js` builds them from `raw_href`.
  - The file is read through the same `_open_beneath` + size + sha256-from-index checks as `serve_bundle_file`. A mismatch gives the 404.
  - Headers: our views' CSP with a fresh nonce, and the other Global Constraints headers. Sent with `cors=False`.
  - An `opened` event `{"file", "mode": "live" | "annotate"}` is written for a GET of the entry.
- **`GET /s/<token>/k/<session>/annotations`.** It returns 200 `{"annotations": [own projection rows]}`, read from `<shares_root>/<share_id>/annotations/<sha256(identity)>.json` with `_open_beneath`. A missing file gives `[]`. Sent with `no-store`, `nosniff` and `cors=False`.
- **`POST .../annotations`** (create), **`POST .../annotations/<aid>/edit`** and **`POST .../annotations/<aid>/delete`.**
  - Each POST is refused unless all of these hold:
    - `Origin == annotate_origin`
    - `X-Herdr-Annotate` passes `check_csrf`
    - `Content-Type` is `application/json`
    - there is exactly one `Content-Length`, from 1 to 32768
    - there is no `Transfer-Encoding`

    Otherwise the response is the uniform 404 with reason `annotate_refused`.
  - Then:
    1. The rate limits are checked, with 429 `{"error": "rate_limited"}`.
    2. The body is read and parsed with `max_chars=annotations["max_comment_chars"]`. A parse error gives 400 `{"error": <code>}`.
    3. **Create:** the anchor's `file` must be in the index, else 400 `bad_file`. One line is appended: `{"v": 1, "op": "create", "annotation_id": "ann_" + token_urlsafe(16), "share_id", "identity", "email", "at", "anchor", "body"}`. The response is 201 `{"annotation": {"annotation_id", "anchor", "body", "status": "submitted", "created_at"}}`.
    4. **Edit / delete:** `<aid>` must match `ANNOTATION_ID_RE`, else 404 `annotate_refused`. It must be an `open` row of the caller's own projection, else 409 `{"error": "not_editable"}`. One line `{"v": 1, "op": "edit" | "delete", "annotation_id", "share_id", "identity", "at", "body"?}` is appended, and the response is 202 `{"annotation_id", "status": "pending"}`.
    5. A failed append gives 503 `{"error": "unavailable"}`.
  - Every POST response is sent with `cors=False`.
- **`OPTIONS`** on any `v/`, `a/` or annotations path gives the uniform 404.
- **`_route`:** POST is accepted only for `session` and the three annotation paths. `_route` recognises `v/` and `a/`, and leaves `f/` on `serve_bundle_file`, unchanged.
- `STATIC_FILES` gains `annotate.js` and `annotate.css`.

- [ ] **Step 1: Write failing tests** (raw `http.client`, as in the existing file)
  - **`a/` for each kind (HTML, text, PNG, PDF, parquet), and `v/` for every kind but HTML:**
    - 200 with a 24-character nonce that differs between requests, and no CORS header.
    - `v/index.html` → 404.
    - The `Live` link of `a/index.html` → `f/index.html`, and of `a/doc.pdf` → `v/doc.pdf`.
    - The mode bar has both links, with the right `aria-current`.
    - `v/` has no CSRF meta, and `a/` has one.
    - With annotations disabled, `a/` is 404 and the bar has `Live` only.
  - **HTML content:**
    - A bundle `index.html` containing `<script>window.x=1</script><iframe src=a></iframe>` is served with neither.
    - A 2 MiB + 1 file gets the too-large message.
  - **The 404 cases:** a v1-format session, a public share, an expired session, a revoked share, and a path not in the index.
  - **The mode-switch link:**
    - A lab `f/index.html` GET with `Sec-Fetch-Dest: document` returns the original bytes with exactly `mode_switch_link(...)` inserted before the last `</body>`. The response has the `-m1` ETag, the correct `Content-Length`, `Vary: Sec-Fetch-Dest`, no `Accept-Ranges`, and the unchanged sandbox CSP.
    - A file without `</body>` gets the link appended.
    - Mixed-case `</BODY>` and a `</body>` inside a comment earlier in the file: the link goes before the *last* one.
    - A nested page `sub/x.html` gets `href="../a/sub/x.html"`.
    - A path containing `&` or `"` is escaped in `href`.
    - The link contains no `<script`, no `on*=`, and nothing matching the no-frames `TAG` regex.
  - **The raw file is unchanged** (golden: bytes, headers, CSP and ETag identical to `main`) for:
    - the same file with `Sec-Fetch-Dest: empty`, `script`, or no such header
    - a `Range` request without `Sec-Fetch-Dest: document`
    - the public share's `f/index.html`
    - a lab share with annotations disabled
    - a lab PDF or PNG on `f/`
  - **POST create:** exactly one line is appended, with a 201. The line's email and identity come from the session, even when the body sends `email`.
  - **POST refusals**, each with no line written:
    - Origin `null`, missing, or another host
    - a missing or wrong CSRF token, or another session's token
    - `text/plain`
    - two `Content-Length` headers, chunked encoding, or 32769 bytes
    - a v1-format session, annotations disabled, a public share, an expired session, or a revoked share
  - **Limits:** the 61st POST in a minute → 429. A file not in the index → 400 `bad_file`.
  - **Edit and delete:**
    - An open own row → 202 and an `op: "edit"` line.
    - Another identity's row → 409.
    - A malformed id → 404.
    - An extra `anchor` key → 400.
  - **Other routes:**
    - OPTIONS on each path → 404.
    - The GET returns only the caller's rows.
    - A full `annotations.jsonl` → 503.
  - **Logging:** no path, CSRF token or nonce reaches a log record.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/control_plane/test_share_public.py tests/control_plane/test_share_lab.py tests/control_plane/test_share_render.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(share): live and annotation views with a mode switch, annotation routes`.

### Task 10: Edge nginx: a dedicated share-host server block, with `/s/` removed from the MCP host

**Files:**
- Modify:
  - `deploy/control-plane/collab-edge/share-http.conf`, which holds the share `server {}` block
  - `deploy/control-plane/collab-edge/nginx.conf`: remove the `@share-location@` splice line
  - `deploy/control-plane/compose.collab.yaml`: mount `${HERDR_SHARE_CERT_DIR}` at `/etc/acme-share:ro`
  - `herdr_engineering/fleet_config_cli.py`: drop the `share-location.conf` splice entry (~line 382); `render_edge_conf` substitutes `@share_host@`
- Delete `deploy/control-plane/collab-edge/share-location.conf`
- Test: extend `tests/test_share_deploy_config.py`

**Interfaces:**
- `render_edge_conf(template, *, share: bool, share_host: str | None = None, snippets_dir=None)`.
  - With `share` true, `share_host` is required: a validated lower-case hostname, with a port allowed.
  - With sharing disabled, the output is byte-identical to the template.
- **`share-http.conf`** keeps today's zones, upstream and maps, and adds:
  - `limit_req_zone $remote_addr zone=share_annotate:10m rate=5r/s;`
  - the `$share_method_ok` entry `"~^POST:/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations(/ann_[A-Za-z0-9_-]{22}/(edit|delete))?\z" 1;`
  - **`server { listen 8443 ssl proxy_protocol; server_name @share_host@; … }`**, with:
    - `ssl_certificate /etc/acme-share/fullchain.pem` and its key
    - HSTS, the `$hcn_from_edge` guard, and `proxy_set_header Host`, `X-Forwarded-*` and `Origin` as in the MCP block
    - `proxy_set_header Sec-Fetch-Dest $http_sec_fetch_dest;`, so the Decision 9 gate sees the browser's value. No `proxy_hide_header Vary`.
    - **flat** locations:
      - `location /s/`: today's `/s/` body, with the method/body guards, `client_max_body_size 1k` and `proxy_pass_request_body off`. It carries `f/`, `v/` and `a/` GETs.
      - `location ~ ^/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations(/ann_[A-Za-z0-9_-]{22}/(edit|delete))?$`, with:
        - GET and POST only (anything else returns 404)
        - `access_log off` and `error_log /dev/stderr crit`
        - `client_max_body_size 32k` and `proxy_pass_request_body on`
        - `limit_req zone=share_annotate burst=30 nodelay` and `limit_conn share_conn 16`
        - `proxy_pass http://share`
      - `location / { return 404; }`
- The MCP host's block has **no** `/s/` location.

- [ ] **Step 1: Write failing tests** on the rendered config:
  - The share block:
    - There is a `server_name artifacts.example.test;` block.
    - The annotations regex location sits at brace depth 2, not nested.
    - It has `proxy_pass_request_body on`, `client_max_body_size 32k` and `share_annotate`.
    - `location /s/` still has `proxy_pass_request_body off`.
    - The share block forwards `Sec-Fetch-Dest`.
  - The MCP block contains neither `location ^~ /s/` nor `proxy_pass http://share`, nor `return 308`.
  - `share-location.conf` does not exist.
  - Rendering:
    - With sharing disabled, the output is byte-identical to the template.
    - `share=True` without `share_host` raises.
  - `nginx -t` passes with dummy certs when `nginx` is on PATH; skip otherwise.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(edge): share host server block; drop /s/ from the MCP host`.

### Task 11: `annotate.js`: mode pages, the annotation panel, capture per kind

**Files:** Create `herdr_engineering/control_plane/share_static/annotate.js`, `annotate.css`, `tests/playwright/share-annotations-server.py`, `tests/playwright/share-annotations.test.mjs`

**Interfaces:**
- `annotate.js` is an ES module loaded by `<script type="module" nonce>`.
  - It reads `#herdr-view-data`, and on `a/` the CSRF meta.
  - It touches no cookie or storage.
  - Its only network calls go to `../annotations` (built from `location.pathname`) and to `raw_href` for images and PDFs.
- **Both modes.** The mode bar is server-rendered, so `annotate.js` adds no navigation. If the browser did not attach the declarative shadow root (`!host.shadowRoot`), the script attaches an **open** one and moves the `<template>` content into it. The content is drawn by kind:
  - **Image:** `<img src=raw_href>` in a positioned box.
  - **PDF:**
    - The script imports `/s/static/vendor/pdfjs/pdf.min.mjs` and sets `GlobalWorkerOptions.workerSrc` to the vendored worker.
    - It calls `getDocument({url: raw_href, isEvalSupported: false, enableXfa: false, disableAutoFetch: true})` and renders only the visible pages (IntersectionObserver).
    - Each page is drawn to `<canvas>` with a text layer and `annotationMode: AnnotationMode.DISABLE`.
    - A load error shows "This PDF could not be drawn".
  - **Document:** the file name and a link to `raw_href`, either to open it or to download it.
- **Annotate mode only.** The panel is an `<aside data-annotate-panel>` with an **open** shadow root, so that Playwright can reach it. Nothing else runs in this document, so a closed root would protect nothing. The panel is built with `createElement` and `textContent` only, and is stacked below the content at ≤ 640px. It holds:
  - the header "Annotating a static copy"
  - the expiry note
  - the current anchor, with a "Whole file instead" reset
  - a `<textarea maxlength=max_comment_chars>` with a counter and Submit
  - "Your annotations", with statuses Open / Addressed in vN / Dismissed / Submitted / Edited. Open rows have Edit (inline Save / Cancel) and Delete (with `confirm()`).
- **Capture** (annotate mode):
  - **HTML / text:** On `selectionchange` (debounced 150 ms) inside the content's shadow root, `getComposedRanges({shadowRoots: [root]})` gives the range. Where that API is missing, the fallback is `getSelection().toString()` plus context from `root.textContent`.
    - `exact` is the selection, clipped to 1000 characters.
    - `prefix` and `suffix` are 64 characters each from around it.
  - **Image:** a pointer-drag box, normalised to the image's natural size in [0, 1].
  - **PDF:** a text-layer selection gives `pdf_page` with `exact`. Alt-drag, or the "Mark region" toggle, gives a `region`.
  - **Document:** a whole-file comment.
- **Submit** sends POST JSON with `X-Herdr-Annotate`, `credentials: "omit"` and `cache: "no-store"`. The button is disabled in flight. Responses:
  - 201/202 → update the list
  - 400 → a fixed message per code
  - 409 → "This annotation can no longer be changed", or "Still saving — try again in a moment"
  - 429 → "Too many annotations — wait a minute"
  - 503 → "Annotations are unavailable right now — try again later"
  - 404 → "Your session ended — open the link again", keeping the text
- **Harness** (`share-annotations-server.py`) runs a real `ShareServer` on 127.0.0.1:
  - It uses a stub authenticator and a fixed secret, sets `annotate_origin` and `public_origin` to its own `http://127.0.0.1:<port>`, and enables annotations.
  - It seeds lab shares:
    - an HTML bundle with `index.html` and `page2.html`, plus a hostile `evil.html` containing scripts, an iframe, `* { display: none !important }` and a fixed full-screen overlay
    - a 3-page PDF
    - a PNG
    - a `.parquet` file
    - a public share
  - It prints `PORT=<n>` and a JSON line of v2 session base URLs for `v/`, `a/` and `f/`, plus the public URL.
  - A separate control port serves `GET /__lines`, `POST /__revoke/<share>` and `POST /__project`.

- [ ] **Step 1: Write the Playwright tests**
  - **Landing and switch:**
    - **HTML lands live.** Signing in through the stub on `/s/<token>/` of the HTML share lands directly on `f/index.html`. The content's own script runs there (a marker it sets is visible), and `[data-herdr-mode-switch]` is visible.
    - **Sandboxed self-navigation works.** A user click on `[data-herdr-mode-switch]` from the CSP-sandboxed top-level `f/index.html` navigates the same tab to `a/index.html`, with no popup, no blocked-navigation console error, and `page.url()` ending in `/a/index.html`. This pins Decision 9's claim that `sandbox` without `allow-top-navigation` still lets a document navigate itself through a plain anchor. Run it in Chromium, and in Firefox and WebKit when the repo's Playwright install has them (skip otherwise).
    - **Back to live.** On `a/index.html`, the mode bar's `Live` link → `f/index.html`, which shows the switch again.
    - **No switch for sub-requests.** On `f/index.html`, `page.evaluate(() => fetch('index.html').then(r => r.text()))` contains no `data-herdr-mode-switch`.
    - **Other kinds land on `v/`.** Signing in on the PDF and PNG shares lands on `v/<entry>`. The content is drawn, the bar shows Live as current, Annotate → `a/<entry>`, and Live → back to `v/<entry>`.
  - **HTML annotation:**
    - Select a sentence; the blockquote shows it.
    - Submit `<img src=x onerror=window.__x=1>` → it is shown literally, `window.__x` is undefined, and `/__lines` has one line with the quote, prefix and suffix.
    - The in-content link to `page2.html` on `a/` → `a/page2.html`, and the next annotation's file is `page2.html`.
  - **`evil.html` on `a/`:**
    - no dialog and no script effect
    - no `iframe`, `frame`, `object` or `embed` element in the DOM
    - the mode bar and the panel are visible and clickable
    - submit still works
  - **`evil.html` on `f/` (the accepted tradeoff):** its CSS may hide the injected link. The test asserts only that `a/evil.html` is still reachable by URL, and that the `f/` response carried the link exactly once.
  - **No forgery from the raw file:** on top-level `f/index.html`, a `fetch` POST to `../annotations` fails, and a `fetch` of `../a/index.html` cannot read its body. `/__lines` is unchanged.
  - **Capture:**
    - The counter blocks the character after `max_comment_chars`.
    - PDF: a page 3 selection → `pdf_page` with `page: 3` and the quote; a region → values in [0, 1].
    - Image: drawing a box → `image_region`.
    - Parquet: a whole-file comment → `document`.
  - **Own rows:** Edit → an `op: "edit"` line. Delete → an `op: "delete"` line after confirm.
  - **Public share:** it lands on raw `f/` with no injected link (`[data-herdr-mode-switch]` count 0), and has no `v/` or `a/` route (404). A fabricated `k/` POST → 404.
  - **Revoke mid-session** → the session-ended message, with the text kept.
  - **Narrow viewport:** at 320px there is no horizontal scroll, and the panel is below the content.
- [ ] **Step 2: Run** `node --test tests/playwright/share-annotations.test.mjs` → FAIL.
- [ ] **Step 3: Implement** `annotate.js` and `annotate.css`.
- [ ] **Step 4: Run** the test plus `python3 -m pytest tests/control_plane/test_share_public.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(share): live/annotate views and the annotation panel`.

### Task 12: Owner annotation API and web-client methods

**Files:** Create `herdr_engineering/control_plane/artifact_annotation_api.py`; Modify `herdr_engineering/control_plane/api.py` (call `route_annotations` right after `route_share`) and `packages/herdr-web-client/client.js`; Test `tests/control_plane/test_artifact_annotation_api.py` and `packages/herdr-web-client/artifact-annotations.test.js`

**Interfaces:**
- `def route_annotations(handler, server, parts, resource) -> bool`:
  - `GET /api/v1/artifacts/{id}/annotations?status=open|addressed|dismissed|withdrawn|all&limit=1..500&before=<cursor>` → `{"artifact_id", "annotations": [...], "next": cursor | null}`. The rows carry the `list_for_artifact` fields plus `author_email`, `author_role` and `share_id`.
  - `POST /api/v1/artifacts/{id}/annotations/{aid}/dismiss` with body `{}` → `{"annotation": {...}}`.
  - Both need the `share` permission (a non-disclosing 404 otherwise) and the same cross-origin POST refusal as `route_share`.
  - Errors: unknown body keys → 400, `not_open` → 409. A POST answered early closes the connection.
- Client: `listArtifactAnnotations(id, { status = "open", before, signal } = {})` and `dismissArtifactAnnotation(id, annotationId, { signal } = {})`, with ids passed through `encodeURIComponent`.

- [ ] **Step 1: Write failing tests**
  - Without `share` → 404.
  - Listing:
    - The status filter works, including `withdrawn`.
    - `limit` 0 or 501 → 400.
    - A malformed cursor → 400.
    - Paging returns `next`.
  - Dismissing:
    - Dismissing an open annotation → 200, it is dismissed in the next list, and an `annotation_dismissed` event is written.
    - Dismissing an addressed or withdrawn annotation → 409.
  - A cross-origin POST → 403.
  - The client methods use the exact paths, methods and bodies.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new tests and `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(annotations): owner list and dismiss API`.

### Task 13: Owner dashboard without iframes (static render, pdf.js, a sandboxed live route)

**Files:**
- Create:
  - `herdr_engineering/control_plane/artifact_render_api.py`
  - `packages/artifact-static-preview.js` and `packages/artifact-static-preview.test.js`
  - `tests/control_plane/test_artifact_render_api.py`
- Modify:
  - `herdr_engineering/control_plane/api.py`: call `route_render` before the `download` branch (~line 3435); `/download` gains `Content-Disposition: attachment` and `Content-Security-Policy: default-src 'none'; sandbox`
  - `herdr_engineering/control_plane/artifact_report_api.py`: `_text` gets `frame-ancestors 'none'` (Decision 8)
  - `packages/herdr-web-client/client.js`
  - `packages/artifacts-view.js`
  - `tests/playwright/artifact-reports.test.mjs`: the `frameLocator` assertions at ~lines 52–57, 217 and 237
  - the Playwright stub server that backs it

**Interfaces:**
- **`route_render(handler, server, parts, resource) -> bool`.** Each route needs the same read permission as `/download` and the outputs route, and gives a non-disclosing 404 otherwise.
  - `GET /api/v1/artifacts/{id}/render` → `{"kind", "html": str | null, "too_large": bool}` for the current file. `kind` is `share_render.render_kind(original_filename, mime_type)`. For `html` the response is `sanitize_html(data, resolve=dashboard_resolver(...))`, and for `text` it is `render_text`.
    - The single-file resolver keeps absolute `http(s)`/`mailto` links and `data:image/(png|jpeg|gif|webp)` images, and drops everything relative.
  - `GET /api/v1/artifacts/{id}/versions/{vid}/outputs/{i}/render` does the same for a legacy report output.
    - Its resolver maps `img`/`link`/`css` references naming another output of the same version to `/api/v1/artifacts/{id}/versions/{vid}/outputs/<j>`.
    - It drops relative `a` links, because the SPA's hash router would be hijacked by them (`showPreview`'s existing rule).
  - `GET /api/v1/artifacts/{id}/versions/{vid}/preview/render` → `{"kind": "html", "html": sanitize_html(render_report_preview(...)), "too_large"}`.
  - `GET /api/v1/artifacts/{id}/live` → the raw bytes under the owner live-route headers (Global Constraints). Auth is the same as `/download`, which today's `<img src>`/`<iframe src>` previews already reach by plain navigation.
  - Responses over `MAX_RENDER_INPUT` give `too_large: true` and no `html`.
  - JSON responses are sent with `no-store` and `nosniff`.
- **Client methods:** `artifactRender(id, {signal})`, `artifactOutputRender(id, versionId, index, {signal})`, `artifactReportPreviewRender(id, versionId, {signal})`, `artifactLiveUrl(id)`, and `artifactBytes(id, {signal}) -> ArrayBuffer` (`/download` with the client headers).
- **`packages/artifact-static-preview.js`:**
  - `mountStaticHtml(container, html, { label })`
    - creates `<div data-static-preview role="document" aria-label=label style="contain:paint;overflow:auto;max-height:640px">`
    - calls `attachShadow({ mode: 'open' })` and writes `shadow.innerHTML = html`. The declarative `<template>` does not attach through `innerHTML`, so the host is attached imperatively.
    - after inserting, unwraps every `a` whose `href` is not absolute `http(s)`/`mailto`, so fragment links cannot drive the hash router
    - shows `STATIC_NOTE` above
  - `mountPdf(container, bytes, { signal })`
    - imports `/vendor/pdfjs/pdf.min.mjs`
    - calls `getDocument({ data: bytes, isEvalSupported: false, enableXfa: false })`
    - renders visible pages to `<canvas>` with `annotationMode: AnnotationMode.DISABLE`
    - shows "This PDF could not be drawn" on error
  - `liveLink(url)` returns `<a class="hd-btn" target="_blank" rel="noopener noreferrer" href=esc(url) data-open-live>Open live view in a new tab</a>`.
- **`artifacts-view.js`:**
  - `showPreview(output, content)` is replaced by `showPreview(output, render)`, which takes a render-route response and calls `mountStaticHtml`. The report preview (~line 324) calls `artifactReportPreviewRender`, and the legacy outputs (~line 186) call `artifactOutputRender`.
  - The ~line 419 ternary becomes:
    - PDF → `mountPdf(await artifactBytes)` + `liveLink`
    - image → native `<img>` (unchanged)
    - HTML → `mountStaticHtml(artifactRender)` + `liveLink`
    - text / other → unchanged
  - No `iframe`, `srcdoc` or `sandbox` attribute remains in the file.

- [ ] **Step 1: Write failing tests**
  - **Python:**
    - Each render route on an HTML artifact holding `<script>`, `<iframe>`, `onerror` and `<a href="#/settings">` returns HTML with none of them.
    - The legacy-output resolver rewrites a sibling image to the outputs route.
    - The `live` route sends the sandbox CSP, `X-Frame-Options: DENY` and no ACAO, and sends a PDF with `frame-ancestors 'none'`.
    - `/download` now sends `attachment` and `sandbox`.
    - `_text` sends `frame-ancestors 'none'`.
    - Without permission → 404.
  - **node:test (`artifact-static-preview.test.js`, with jsdom-free stubs as in the existing component tests):**
    - Relative and fragment links are unwrapped, and absolute links are kept.
    - The host has `contain:paint`.
    - `liveLink` escapes the URL and has `rel="noopener noreferrer"`.
  - **Playwright (`artifact-reports.test.mjs`, rewritten):**
    - "Load lightweight preview" shows `[data-static-preview]`, and Playwright locators pierce the open shadow root to find heading `Findings`.
    - There is no `iframe` in the page (`page.frames().length === 1`).
    - The legacy version's heading is shown the same way at the old 217/237 sites.
    - The URL stays `#/artifacts/report_1` after clicking inside the preview.
    - A PDF artifact draws at least one non-blank canvas.
    - An HTML artifact with `<script>window.__x=1</script>` leaves `window.__x` undefined.
    - The live link opens a popup whose `document.domain` access throws, or whose `origin` is `"null"`: it is the sandbox.
- [ ] **Step 2: Run** `python3 -m pytest tests/control_plane/test_artifact_render_api.py -q`, `node --test packages/artifact-static-preview.test.js` and `node --test tests/playwright/artifact-reports.test.mjs` → FAIL.
- [ ] **Step 3: Implement.** If the dashboard CSP blocks pdf.js font loading in the Playwright run, add the narrowest needed source (`font-src 'self' data:`) to `_PUBLIC_CSP`, with a test. Never add `frame-src`.
- [ ] **Step 4: Run** those, plus `npm run test:client` and `python3 -m pytest tests/control_plane/test_artifact_report_render.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(web): replace dashboard preview iframes with static render and pdf.js`.

### Task 14: Feedback panel in the artifact detail view

**Files:** Create `packages/artifact-feedback-panel.js` and `packages/artifact-feedback-panel.test.js`; Modify `packages/artifacts-view.js` (mount it in `renderArtifactDetail` and `renderReportReader` next to `mountSharePanel`) and `packages/artifact-share-panel.js` (`renderEvents` labels the five new kinds)

**Interfaces:**
- Consumes the Task 12 client methods.
- `export function renderAnchor(anchor)`:
  - `text_quote` → `…prefix<mark>exact</mark>suffix…`, escaped
  - `pdf_page` → `Page N`, plus the quote and the region when present
  - `image_region` → `Region x%, y% (w% × h%)`
  - `document` → `Whole file`
  - always followed by `in <code>file</code>`
- `export function renderAnnotationRow(annotation, { artifactId, busy })` returns escaped HTML with:
  - the email, plus an "owner" badge
  - `vN`, the anchor, and an "edited" marker
  - the body in `<p style="white-space:pre-wrap;overflow-wrap:anywhere">`
  - the status
  - the `addressed_in_version` link, in the report reader's version-link format (`artifacts-view.js` ~line 212)
  - Dismiss, only when `status === 'open'`
- `export function mountFeedbackPanel(container, client, artifactId, { signal }) -> { dispose() }`:
  - a status filter (Open by default) and "Load more"
  - a per-section `<herdr-error>` with Retry
  - Dismiss asks `confirm()`, disables the button in flight, and refreshes
- If plan 3's Share card has a tab bar, the panel mounts into a "Feedback" tab panel. Otherwise it mounts as a `<section data-artifact-feedback>` after the Share card.

- [ ] **Step 1: Write failing tests**
  - Hostile emails, bodies, quotes and file names are escaped.
  - Open rows have Dismiss. Addressed rows link to vN and have no Dismiss.
  - The owner badge and the edited marker are shown.
  - A 4000-character unbroken body gets the wrap style.
  - The empty state reads "No annotations yet. Lab viewers can annotate a shared version by switching a lab link to Annotate; public links cannot."
  - `renderEvents` shows `annotated` as "annotated" and `annotation_withdrawn` as "annotation deleted by its author".
- [ ] **Step 2: Run** `node --test packages/artifact-feedback-panel.test.js` → FAIL.
- [ ] **Step 3: Implement** and mount.
- [ ] **Step 4: Run** `npm run test:client` → PASS.
- [ ] **Step 5: Commit** `feat(web): feedback panel for artifact annotations`.

### Task 15: MCP tools `artifact_annotations` and `artifact_annotation_resolve`, through plan 3's registry

**Files:**
- Create:
  - `herdr_engineering/control_plane/annotation_intents.py`
  - `tests/control_plane/test_collab_mcp_annotations.py`
  - `tests/control_plane/test_annotation_resolve_intent.py`
- Modify:
  - `herdr_engineering/control_plane/collab_mcp.py`: two tools
  - `collab_artifacts.py`: `CollabArtifactBridge.annotations` / `.resolve_annotations`; it imports `annotation_intents` so that the kind is registered in collab-public
  - `collab_mirror.py`: `publish_annotations`
  - `herdr_engineering/control_plane/worker.py`: `AnnotationHousekeeper` publishes for touched artifacts
  - `herdr_engineering/cli.py`: in `_cmd_cp_worker`, `register_annotation_resolve(applier)` next to plan 3's `commit` registration
  - `packages/collab-guide.md`, `docs/operations/collaboration-network-guide.md`, and `skills/herdr-artifact/SKILL.md` (the tool list only)

**Interfaces:**
- **`annotation_intents.py`.** It is light: its import pulls in no asyncpg-heavy modules, because collab-public imports it.
  - `@dataclass(frozen=True) class AnnotationResolvePayload: version_id: str; annotation_ids: tuple[str, ...]`.
  - `def validate_annotation_resolve_payload(raw: dict, limits: BridgeLimits) -> AnnotationResolvePayload`. It accepts exactly the keys `{version_id, annotation_ids}`, with 1–50 unique ids that each match `ANNOTATION_ID_RE`. Anything else raises `IntentError`.
  - At import it calls `register_intent_kind("annotation_resolve", validate_annotation_resolve_payload, actors=frozenset({"agent"}))`. `artifact_id` and `idempotency_key` travel in plan 3's envelope, not in the payload.
  - `async def handle_annotation_resolve(ctx: IntentContext, intent: Intent) -> dict`:
    1. If `intent.artifact_id` or `intent.idempotency_key` is None → `{"result": "rejected", "reason": "bad_envelope"}`.
    2. `await ctx.grants.grant_for(intent.artifact_id, ctx.actor.id)` must give `write`, unexpired, else `rejected` with "no write grant". The registry's generic steps do not check grants.
    3. The version must belong to the artifact (`ctx.store.get_version`), else `bad_version`. It must have been committed by this agent (`created_via = 'collaborator' AND collaborator_agent_id = ctx.actor.id`), else `not_own_version`.
    4. It calls `AnnotationStore(ctx.conn).resolve(..., actor=f"collab:{ctx.actor.id}")`. `artifact_annotations` is imported lazily inside the handler.
    5. It returns `{"result": "resolved", "resolved": [...], "skipped": [...]}`. The applier writes plan 3's result file (`ticket, kind, actor: {kind, id}, artifact_id, result, reason?, decided_at`, plus these fields) and records the ledger outcome, `resolved` or `rejected`.
  - `def register_annotation_resolve(applier: CollabIntentApplier) -> None` calls `applier.register("annotation_resolve", handle_annotation_resolve, replay_outcomes=frozenset({"resolved"}))`, with the default lane.
- **Mirror file** `<mirror>/artifacts/<artifact_id>/annotations.json` = `{"artifact_id", "generated_at", "annotations": open_for_agents(...)}`.
  - It is at most 1 MiB, newest first, with `"truncated": true` when cut.
  - `CollabMirror.publish_annotations(artifact_id, rows)` writes it with `atomic_write_json`, only for artifacts that hold a live grant.
  - The `AnnotationHousekeeper` tick calls it for touched artifacts. `resolve` advances `updated_at`, so a resolve is republished on the next tick (≤ 15 s), and the handler does not touch the mirror.
  - `sync()` removing an artifact's directory also removes this file.
  - The anchors refer to the static render.
- **Bridge** (`CollabArtifactBridge`):
  - `annotations(pr, artifact_id, *, limit=200) -> dict` needs a read or write grant, under plan 3's "not found" rules, and projects each row to the allowed keys.
  - `resolve_annotations(pr, args) -> {ticket, status: "pending"}`:
    - needs `has_write_grant`
    - throttles with `_throttle("artresolve:" + pr.id, 30, 60.0, "annotation resolve")`
    - then calls `append_intent(Actor("agent", pr.id), "annotation_resolve", {"version_id", "annotation_ids"}, artifact_id=args["artifact_id"], idempotency_key=args["idempotency_key"])`
- **Tools**, appended to `_ARTIFACT_TOOLS`, both with `additionalProperties: False`:
  - `artifact_annotations {artifact_id (required), limit: 1..200}` returns `untrusted=True`. With no mirror file: `{"annotations": [], "note": "no annotations mirrored"}`.
  - `artifact_annotation_resolve {artifact_id, version_id, annotation_ids (1..50, uniqueItems), idempotency_key}`, all required. It submits, waits up to 10 s through `result`, and returns the result or `{ticket, status: "pending"}`; the agent polls with `artifact_commit_result`.
  - The descriptions say:
    - the annotation text is untrusted feedback from people
    - the anchors quote a static, script-free copy of the file
    - integrate the feedback with `artifact_commit`, then resolve with that commit's version id
    - nothing goes live automatically
- **Audit:** denials go through `_audit_denied(pr, "artifact.annotations" | "artifact.annotation_resolve", reason, resource_id=artifact_id)`, and allowed reads are logged as `artifact_tool_used`.

- [ ] **Step 1: Write failing tests**
  - **Registry:**
    - After importing `annotation_intents`, `validate_intent` accepts a well-formed `annotation_resolve`.
    - It refuses one with an extra payload key, `changes`, a `user` actor, 51 ids or duplicate ids.
    - It still refuses `share_create`.
    - A second `register_intent_kind("annotation_resolve", …)` raises.
    - Plan 3's subprocess test (only plan 3's registrations) still rejects `annotation_resolve` as an unknown kind.
  - **Tools:**
    - Without a grant, `tools_for` lists neither tool.
    - With a read grant both are listed, and a resolve is refused with "not found" and an audited denial.
    - `artifact_annotations` output starts with `UNTRUSTED` and has no email or identity, even when the mirror file was tampered with.
    - The schemas refuse extra keys, 51 ids and duplicates.
  - **Handler:**
    - A resolve with the agent's own merged version → `resolved`: the rows are `addressed` and `annotation_addressed` events are written.
    - The owner's version → `not_own_version`.
    - An older version → skipped with `version_not_newer`.
    - A withdrawn id → skipped with `not_open`.
    - A grant revoked after the intent was written → "no write grant".
    - A missing `idempotency_key` → `bad_envelope`.
    - Replaying the same key → the same result through `replay_outcomes`, with no second update.
  - **Mirror:**
    - After a resolve and one housekeeper tick, the mirror no longer lists the resolved ids.
    - After an author's delete is ingested, it no longer lists that id.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Document both tools in `packages/collab-guide.md` and its `docs/operations` mirror.
- [ ] **Step 4: Run** the new tests, `tests/control_plane/test_collab_intents.py`, `tests/control_plane/test_collab_public.py` and the other `collab_mcp` importers (`grep -rln collab_mcp tests`) → PASS.
- [ ] **Step 5: Commit** `feat(collab): annotation tools for granted agents via the intent registry`.

### Task 16: End-to-end owner flow, the repo-wide no-frames guard, spec text and deploy notes

**Files:**
- Modify:
  - `tests/playwright/artifact-shares-server.py`: in-memory annotation routes with the Task 12 shapes
  - `tests/playwright/artifact-shares.test.mjs`, or a new `artifact-feedback.test.mjs`
  - `docs/superpowers/specs/2026-10-30-artifact-sharing-gui-and-collaboration-design.md` (section D)
  - the share deploy doc under `docs/operations/` (`grep -rl "control_plane.share" docs/operations`)
- Create `tests/control_plane/test_no_frames.py`

- [ ] **Step 1: Write the Playwright test**
  - The artifact detail page shows the Feedback section with three seeded annotations: one open with a hostile body, one addressed in v3, and one open with `edited_at`.
  - The hostile body is literal, and `window.__x` stays undefined.
  - The addressed row links to v3, and the edited row shows "edited".
  - Dismiss moves the row from Open to Dismissed. A double click sends one POST.
  - A 500 shows the error with Retry, and Retry recovers.
  - At 320px there is no horizontal scroll.
- [ ] **Step 2: Write** `test_no_frames.py`, with `TAG = re.compile(r"<\s*(iframe|frame|frameset|object|embed|portal|fencedframe)(?=[\s/>])", re.I)` and `API = re.compile(r"createElement\(\s*['\"`](iframe|frame|object|embed)|\.srcdoc\b|\bsrcdoc=|postMessage\(", re.I)`:
  - **(a) Source scan.** Every `.js`, `.mjs`, `.html` and `.css` file under `packages/` is scanned, along with `herdr_engineering/control_plane/share_static/` and every `.py` under `herdr_engineering/`. Excluded are `node_modules`, `*.test.js` and `share_static/vendor/`. No file matches `TAG` or `API`.
    - The detector byte strings in `intel_attachments.py` (`b"<iframe"`) are not tags and do not match.
    - Any new exception must be a named entry in an `ALLOWED` dict with its reason. The dict starts empty.
  - **(b) Rendered responses.** None of these response bodies matches `TAG` once lower-cased. The `MODE_SWITCH_TEMPLATE` constant must also match neither `TAG` nor `API`:
    - the dashboard `index.html` and every `_PUBLIC_STATIC` HTML page
    - the share viewer page and `/s/callback`
    - the static assets
    - `v/` and `a/` for every kind, including a bundle full of frames
    - the annotations JSON
    - the owner render routes and the report preview
  - **(c) The sanitizer on a corpus of frame and plugin markup:** `<IFRAME`, `<iframe/src=…>`, `<frameset><frame>`, `<object><embed>`, `<svg><foreignObject><iframe>`, `<portal>`. Every output matches nothing.
  - The raw `f/` route and the owner `live` route serve raw content and are excluded from (b) by design.
- [ ] **Step 3: Run** `npm run test:ui` for the file and the new pytest → FAIL, then implement the stub routes until PASS. Fix product code if the guard finds a defect.
- [ ] **Step 4: Update the spec**, section D:
  - Replace the shell / injected script / `postMessage` capture with the live and annotation views: the mode switch, the static render through nh3, the nonce CSP, the shadow root and the CSRF token.
  - Add the edit/delete rule, the `withdrawn` status, the raised limits, the `pdf_page` region and the share host.
  - Add a constraint "No frames anywhere, dashboard included (operator 2026-10-30)".
- [ ] **Step 5: Write the deploy notes.** These are operator steps; the implementer does not run them:
  1. DNS: point `artifacts.metabolomics.us` at the public edge.
  2. Add the hostname to the cubert SNI stream edge in `networking/cubert-ansible`, routed to the collab edge like `localdev.mcp.metabolomics.us`.
  3. Certificate: run `deploy/control-plane/collab-sync.sh install --cert-name artifacts.metabolomics.us --cert-dir <dir>`, and set `control_plane.compose.HERDR_SHARE_CERT_DIR: <dir>`.
  4. In Cognito app client `cognito_web_client`, set the callback URL `https://artifacts.metabolomics.us/s/callback` and the sign-out URL `https://artifacts.metabolomics.us/`. Remove the MCP host's `/s/callback` entry.
  5. In YAML, set `control_plane.share.public_url: https://artifacts.metabolomics.us`, plus optional `owner_emails` and `annotations` overrides.
  6. Rebuild the image (new `nh3` dependency and vendored pdf.js). Redeploy collab-edge, share-public and the control plane, then apply migration 0045.
  7. Smoke-check:
     - An MCP-host `/s/...` path no longer reaches share-public.
     - A lab link to an HTML artifact signs in and lands directly on the running page, with the Annotate link. Clicking it opens the annotation view, and Live returns to the page.
     - Annotate → a test annotation reaches the Feedback panel.
     - The dashboard shows HTML and PDF previews with no frame.
- [ ] **Step 6: Run** the UI gates (`npm run test:ui-gates`), and update intentionally changed baselines by the repo's procedure. The dashboard preview baselines change because the iframes are gone. A baseline already red on main is not this plan's to touch.
- [ ] **Step 7: Run** `python3 -m pytest tests/control_plane -q`, `tests/test_share_deploy_config.py` and `npm test` → PASS.
- [ ] **Step 8: Commit** `test(annotations): owner feedback end-to-end, repo-wide no-frames guard, spec and deploy notes`.

---

## Self-review notes

- **Spec coverage (section D, as amended by the Decisions):**

  | Requirement | Tasks |
  |---|---|
  | who may annotate | 6, 9 |
  | anonymous/public refused | 9, 11 |
  | anchors | 1, 7, 11 |
  | capture without frames (static render, nonce CSP, shadow root) | 7, 9, 11 |
  | lab links land directly on the live view (every type), with the Live↔Annotate switch: injected link on HTML `f/`, mode bar on `v/`/`a/` | 6, 9, 11 (Decision 9) |
  | annotation UI | 11 |
  | session-authenticated, CSRF-checked, size-capped, rate-limited POST | 6, 9, 10 |
  | plain text, never HTML | 1, 11, 14, 15 |
  | database-free append plus worker ingest | 2–4 |
  | statuses and `addressed_in_version_id` | 3 |
  | edit/delete own annotations while open | 1, 3, 4, 9, 11 |
  | annotations file rules | 2, 4, 9 |
  | owner Feedback panel | 12, 14, 16 |
  | annotators' own read-back | 4, 9, 11 |
  | MCP tools via the registry | 15 (Decision 12) |
  | a new revision never changes a live link | Global Constraints |
  | the owner annotates through the lab link | 4 |
  | share host from YAML, MCP-host `/s/` removed | 5, 10, 16 |
  | no frames anywhere, dashboard included | 7, 13, 16 (Decision 1) |
  | libraries, not hand-rolled code (nh3, pdf.js) | 7, 8 (Decision 10) |

- **Removed in this revision (operator 2026-10-30):**
  - the MCP host's `308` and `share-location.conf`
  - the "old links keep working" tests and smoke check
  - the old host's Cognito callback
  - v1 session acceptance
  - the `#annotate` fragment, the `share.js` hash mode, and the Share card's "Annotation link"
  - the hand-rolled `html.parser` sanitizer, with its `MAX_ELEMENTS`/`MAX_DEPTH`/`OMITTED` constants
  - the widened `Intent.kind` `Literal`, the custom `AnnotationResolveIntent`, and the handler's own mirror write
  - the out-of-scope entry "replacing the dashboard preview iframes", which is now Task 13
  - operator questions 1–4, which are resolved
  - the `v/` wrapper page for HTML (its "Open the live page" link and the static copy under it), and the open question about it. HTML now lands directly on `f/` with an injected link (Decision 9).
- **Removed in the earlier draft:** the shell route `k/<session>/`, the sandboxed iframe and the PDF frame, `postMessage` intake, HTML *script* injection and its `-a1` ETag (the new injection is a script-free link with an `-m1` ETag), `__PARENT_ORIGIN__`, `frame-ancestors 'self'`/`frame-src 'self'`, and the nested nginx location.
- **Out of scope:**
  - "share latest"
  - members reading their own feedback via MCP (plan 5)
  - highlighting existing annotations inside the content
  - annotating the running (scripted) page
  - a markdown renderer
  - an inline SVG allow-list
- **Renumbering** (old → new): 1–7 → 1–7; new 8 (pdf.js vendoring, split out of old 10); 8 → 9; 9 → 10; 10 → 11; 11 → 12; new 13 (dashboard iframes); 12 → 14; 13 → 15; 14 → 16.
  - The cross-references in Review Focus, the spec coverage table and the task bodies use the new numbers.
  - Order holds: the nh3 adapter (7) and pdf.js (8) come before both consumers (9, 11 and 13).
- **Consistency fix:** the annotation panel's shadow root is **open**, not closed. Nothing else runs in that document, and Playwright cannot pierce a closed root. The dashboard preview root is open for the same reason.
- **Names used across tasks:**
  - `annotation_anchor.{parse_annotation_request, parse_edit_request, parse_delete_request, ANNOTATION_DEFAULTS, ANNOTATION_ID_RE}`, `AnnotationInputError`
  - `JsonlIngestor`
  - `AnnotationStore.{ingest, list_for_artifact, dismiss, resolve, own_rows, open_for_agents, touched_since}`, `AnnotationHousekeeper`, `write_own_projection`, `annotation_settings`
  - `share_session.{mint_session(…, email), check_session, csrf_token, check_csrf}`, with the session response key `landing`
  - `share_public.{MODE_SWITCH_TEMPLATE, mode_switch_link, inject_mode_switch}`, `serve_bundle_file(…, mode_switch=)`, ETag suffix `-m1`, DOM hook `data-herdr-mode-switch`
  - `share_render.{render_kind, sanitize_html(data, *, resolve), share_resolver(name, index), render_text, wrap_content, RenderCache, RenderTooLarge, STATIC_NOTE}`
  - `render_edge_conf(..., share_host=)`
  - `route_annotations`, `route_render`
  - `listArtifactAnnotations`, `dismissArtifactAnnotation`, `artifactRender`, `artifactOutputRender`, `artifactReportPreviewRender`, `artifactLiveUrl`, `artifactBytes`
  - `mountStaticHtml`, `mountPdf`, `liveLink`, `mountFeedbackPanel`
  - `annotation_intents.{AnnotationResolvePayload, validate_annotation_resolve_payload, handle_annotation_resolve, register_annotation_resolve}`, intent kind `annotation_resolve`, `CollabMirror.publish_annotations`
  - JSONL `op` values `create | edit | delete`; files `annotations.jsonl` and `annotations.json`; template `view.html`
  - routes `k/<session>/{f,v,a}/<path>` and `k/<session>/annotations[/<aid>/(edit|delete)]`
  - owner routes `/api/v1/artifacts/{id}/{render,live}`, `…/versions/{vid}/outputs/{i}/render`, `…/versions/{vid}/preview/render`
- **Proportion:** 16 tasks. Task 7 now configures a library instead of writing a parser, so it carries a Step 0 spike instead. Task 13 is the largest new task, because it touches three dashboard preview sites and their tests.
- **Operator questions:** none open. The HTML landing question was resolved on 2026-10-30 (Decision 9).
