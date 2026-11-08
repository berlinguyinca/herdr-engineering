# Dashboard Artifact Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opening an artifact in the dashboard shows it running first (opaque-origin sandbox, scripts allowed), under a slim top bar with version picker, share, open public page, download and fullscreen, and lets the operator create a live share link.

**Architecture:** The internal listener serves artifact bytes under the public sandbox CSP and gains one write route (create share, separate write key). cp-web mints a short-lived HMAC-signed content URL per (item, version) and serves the bundle by prefix from a route exempt from the API-key gate, proxying the host's content. The item page is a frame-first view with a closed metadata drawer.

**Tech Stack:** Python 3.13, aiohttp + asyncpg (host), stdlib `http.server` (cp-web), nginx relay, vanilla ES modules in `packages/`, pytest with real Postgres, Playwright (`node --test`).

**Spec:** `docs/superpowers/specs/2026-11-08-dashboard-artifact-viewer-design.md` (approved, authoritative); amends `docs/superpowers/specs/2026-11-06-artifact-host-app-design.md`.

**Sequencing:** This plan runs AFTER Task 12 of `docs/superpowers/plans/2026-11-06-artifact-host-app.md` (host deployed, internal listener and relay live). Task 13 of that plan (dead-code removal) is independent.

## Global Constraints

- TDD: failing test, run it red, implement, run it green, commit.
- NO mocks, stubs or fakes: real Postgres (existing `tests/artifact_host` fixtures), real HTTP servers, real docker where the existing compose tests use it.
- Playwright E2E runs as `node --test tests/playwright/*.test.mjs` (one file at a time while developing); never Playwright MCP.
- Light theme only; no dark mode.
- Wire format MessagePack with JSON fallback exactly as today (`Accept: application/json` gets JSON); browsers get JSON.
- `herdr_artifact_host` never imports `herdr_engineering`; the shared constant is duplicated and pinned by a drift test, not imported.
- Run test files one at a time: `uv run --extra dev --extra collab --extra federation pytest <file> -q`.
- Never `git add` `.superpowers`, `*.egg-info`, `test-results`, `node_modules`, `uv.lock`; add files by explicit path only. Never commit to main.
- Commit trailer on every commit: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Frame is `sandbox="allow-scripts allow-downloads"`, never `allow-same-origin`. Dashboard is otherwise read-only: exactly ONE non-GET route on the internal listener and the relay, exactly ONE POST in the dashboard library API (create share).
- Share expiry: default 30 days, max 90. A share token is returned only by the create response, never by any list or read response, never logged.
- Signed content URL: HMAC key lives in cp-web only, never sent to the host; the URL grants read of one item+version for minutes.

## Review Focus

1. Artifact script reading or writing the dashboard (parent DOM, cookies, storage, `fetch` of `/api/v1/...`): frame must see an opaque origin and be refused. Pinned in Tasks 5 and 10.
2. Signed URL replayed against another item/version, after expiry, tampered, or with `..`, `%2e%2e`, `%2f`, double-encoded or NUL segments: 404/403, never a file. Task 5.
3. The signature-exempt prefix route swallowing a neighbour (`/api/v1/library-content-x`, `/api/v1/library/...`): only the exact prefix is exempt. Task 5.
4. Read key used on the POST route, or write key used on GET routes: both refused, same bytes as a wrong key. Task 2.
5. Sharing an item whose member was removed, whose version is purged/draft, or that is retracted: refused with no row and no event written. Task 1.

---

## File Structure

- Create `herdr_artifact_host/operator_shares.py`: the create-share rules and SQL (one responsibility; does not live in `member_api.py`, which stays owner-scoped).
- Modify `herdr_artifact_host/internal_server.py`, `host_config.py`, `roles.py`, `cli.py`, `migrations/` (new `0008_internal_share_grants.sql`), `deploy/artifact-host/*`.
- Create `herdr_engineering/control_plane/library_content_token.py` (sign/verify only) and `artifact_library_content_api.py` (mint + prefix route); modify `artifact_host_client.py`, `artifact_library_api.py`, `api.py` (route wiring only).
- Modify `packages/library-frame.js`, `packages/library-view.js`, `packages/herdr-web-client/client.js`; create `packages/library-share-dialog.js` and `packages/library-fullscreen.js` (small, separately testable).

---

### Task 1: Host operator-share service

**Files:**
- Create: `herdr_artifact_host/operator_shares.py`, `herdr_artifact_host/migrations/0008_internal_share_grants.sql`
- Modify: `herdr_artifact_host/roles.py` (internal role grants), `deploy/artifact-host/roles.sql` or wherever `roles.sql` is rendered (grep `GRANT` under `herdr_artifact_host/` and `deploy/`)
- Test: `tests/artifact_host/test_operator_shares.py`

**Interfaces:**
- Consumes: `member_api` share rules by value: `DEFAULT_SHARE_DAYS` (30), `MAX_SHARE_DAYS` (90), `share_store.token_digest(token) -> str`, `share_bundle.choose_entry`.
- Produces:
  - `class OperatorShareError(Exception)`: `status: int`, `code: str`
  - `@dataclass(frozen=True) class CreatedShare`: `share_id: str`, `token: str`, `url: str`, `expires_at: datetime`
  - `async def create_operator_share(pool: asyncpg.Pool, *, artifact_id: str, version_id: str, expires_in_days: int | None, operator: str, public_origin: str | None, now: Callable[[], datetime]) -> CreatedShare`
  - Migration `0008`: `GRANT INSERT ON shares, share_events` and `SELECT` on `member_usage` to the internal role only (no UPDATE/DELETE anywhere).

- [ ] **Step 1: Write failing tests** in `test_operator_shares.py` (real Postgres via the existing `host`/`migrated_dsn` fixtures, using the `internal` DB role pool):
  - `test_creates_a_live_public_share_for_the_owner`: row has `state='live'`, `audience='public'`, `created_by == artifact owner`, `approved_by == "operator:alice"`, `token_hash == token_digest(token)`, `expires_at` = now + 30 d; one `share_events` row `kind='created'` with `actor == "operator:alice"` and `detail->>'via' == 'dashboard'`; the token is not stored anywhere in clear (grep every text column of both tables).
  - `test_expiry_bounds`: `None`->30 d, `1` and `90` ok; `0`, `91`, `True`, `"5"`, `1.5` -> `OperatorShareError(400, "invalid_expiry")`.
  - `test_refusals_write_nothing` (parametrized): unknown item 404 `not_found`; lifecycle `retracted` and `tombstoned` 404; intake `discarded` 404; version `purged` and `draft` 409 `version_not_ready`; version of another artifact 404; member with `member_usage.removed_since` set 403 `member_removed`. Each asserts `count(shares)` and `count(share_events)` unchanged.
  - `test_appears_in_the_owner_share_list`: `MemberService` share listing for the owner includes the new share as live.
  - `test_internal_role_cannot_update_or_delete_shares`: raw `UPDATE shares` / `DELETE FROM share_events` as the internal role raises `InsufficientPrivilegeError`.
- [ ] **Step 2: Run** `uv run --extra dev pytest tests/artifact_host/test_operator_shares.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `create_operator_share` in one transaction (`SELECT ... FOR SHARE` on the artifact row so a concurrent retract serialises); entry via `choose_entry(None, names)`; `file_count`/`total_bytes` from the manifest as `member_api._share_create_once` does; token `secrets.token_urlsafe(32)`, share id `"shr_" + secrets.token_hex(12)`; URL `f"{public_origin}/s/{token}/"` (empty origin -> relative). No per-member rate limit and no `sharing_disabled` bypass: if `member_usage.sharing_disabled` is true refuse 403 `sharing_disabled` (operator must not override an owner-level block silently).
- [ ] **Step 4: Implement migration 0008 and grants**; re-run the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(artifact-host): operator share service and minimal internal-role grants`.

### Task 2: Internal listener write key and the one POST route

**Files:**
- Modify: `herdr_artifact_host/internal_server.py` (`InternalConfig`, `handle`, `route`), `herdr_artifact_host/host_config.py` (`internal_write_key_digest`, internal slice), `herdr_artifact_host/roles.py` (route table), `herdr_artifact_host/cli.py:112`
- Test: `tests/artifact_host/test_internal.py` (update `test_non_get_is_405`, `test_route_tables_are_disjoint_and_complete`, `test_read_only_pool_refuses_writes`, `test_a_full_read_sweep_writes_nothing`), `tests/artifact_host/test_internal_share_create.py` (new)

**Interfaces:**
- Consumes: Task 1 `create_operator_share`.
- Produces:
  - `InternalConfig.write_key_digest: str = ""` (empty = route disabled -> 404 `not_found`); validated like `key_digest`, must differ from `key_digest`.
  - Route `POST /v1/items/<item>/versions/<version>/shares`, body `{"expires_in_days"?: int}` (MessagePack or JSON), header `X-Herdr-Operator: <id>` matching `^[A-Za-z0-9:._@-]{1,128}$` (missing/invalid -> 400 `invalid_request`). Response 201 `{"share_id","token","url","expires_at"}` with `Cache-Control: no-store`.
  - Auth: the POST route accepts ONLY the write key; GET routes accept ONLY the read key. Wrong/missing key on either is the same 401 bytes and counts in the same failed-key limiter.

- [ ] **Step 1: Write failing tests** (real listener + Postgres, as `test_internal.py` does):
  - `test_read_key_cannot_create_a_share` and `test_write_key_cannot_read`: 401 identical to a wrong key; no share row.
  - `test_create_returns_token_once`: 201 with token; then `/v1/shares`, `/v1/items/<id>`, `/v1/feed` bodies (msgpack and JSON) contain neither the token nor its digest.
  - `test_only_that_route_accepts_post`: POST/PUT/DELETE/PATCH to every other path (including `/v1/shares`, `/v1/feed`, `/v1/content/...`, `/internal/healthz`) is 405 `Allow: GET`; PUT/DELETE/PATCH on the share route is 405 `Allow: POST`.
  - `test_body_rules`: body > `MAX_BODY` 413/400, invalid msgpack 400, `expires_in_days` bounds 400, refusals from Task 1 surface with their status and code.
  - `test_unconfigured_write_key_makes_the_route_404`.
  - `test_the_read_sweep_still_writes_nothing`: the existing sweep proof stays green (GET path unchanged).
  - Update `test_non_get_is_405` to exclude the one POST route, and the route-table test to list `POST /v1/items/{id}/versions/{id}/shares` under `Role.INTERNAL` only (public role still 404 on it).
- [ ] **Step 2: Run** each file separately. Expected: FAIL.
- [ ] **Step 3: Implement.** Keep `ReadOnlyPool` for GETs; the write route uses a second `asyncpg.Pool` handle (`InternalSurface.writer`) built by `cli.py` from the same internal DB role. Bearer parsing is shared; `authorised(request, *, write: bool)` compares against the matching digest only.
- [ ] **Step 4: Run** the files. Expected: PASS. Also run `tests/artifact_host/test_roles.py` if present.
- [ ] **Step 5: Commit** `feat(artifact-host): internal create-share route behind a separate write key`.

### Task 3: Internal content under the public sandbox CSP

**Files:**
- Modify: `herdr_artifact_host/internal_server.py:69-77,599`, `herdr_artifact_host/share_http.py` (export a `dashboard_content_csp()` helper next to `sandbox_csp`)
- Test: `tests/artifact_host/test_internal.py` (`test_content_is_served_sandboxed`, `test_csp_on_every_response`)

**Interfaces:**
- Produces: `share_http.dashboard_content_csp() -> str` equals `sandbox_csp(None, None)` with `frame-ancestors 'none'` replaced by `frame-ancestors 'self'` (the dashboard must be able to frame it; the public policy forbids framing). Everything else byte-identical to the public template. Content responses carry exactly this. Non-content responses (API, errors) carry `API_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"`. `CONTENT_CSP` is deleted.

- [ ] **Step 1: Write failing tests:** content response `Content-Security-Policy == dashboard_content_csp()`; it starts with `sandbox allow-scripts allow-downloads;`, contains no `allow-same-origin`, `allow-forms`, `allow-popups`, `allow-top-navigation`; differs from `share_http.CSP` only in `frame-ancestors`; every non-content response (feed, 401, 404, 405, 429) carries `API_CSP`; `rg CONTENT_CSP herdr_artifact_host herdr_engineering tests` finds nothing (asserted by a test reading sources).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** and remove `CONTENT_CSP`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(artifact-host): internal content uses the public sandbox policy`.

### Task 4: Relay and compose

**Files:**
- Modify: `deploy/artifact-host/relay/nginx.conf`, `deploy/artifact-host/compose.yaml`, `herdr_engineering/cli.py` render-artifact-host and `deploy/control-plane/deploy.sh` (render `internal_write_key_digest` into `internal.json` from `control_plane.artifact_library.host_write_key`)
- Test: `tests/test_artifact_host_compose.py` (`test_the_relay_allows_only_get_and_nothing_outside_v1`, `test_the_relay_is_a_pinned_hardened_one_way_nginx`), plus its docker smoke test if one exists there

**Interfaces:**
- Produces: nginx `location ~ ^/v1/items/[A-Za-z0-9_-]+/versions/[A-Za-z0-9_-]+/shares$ { limit_except POST { deny all; } ... client_max_body_size 1k; }` placed before `location /v1/`; the generic `/v1/` block stays `limit_except GET`. Fleet config gains `control_plane.artifact_library.host_write_key` (>= 32 chars, must differ from `host_key`); only its sha256 is written to the host slice.

- [ ] **Step 1: Write failing tests:** relay conf has exactly two proxying locations, the generic one `GET`-only and the share one `POST`-only; `POST` appears in code exactly once; the share location regex rejects `/v1/items/a/versions/b/shares/x`, `/v1/items//versions/b/shares`, `/v1/shares`, `/v1/items/a/shares`; `proxy_pass` set is unchanged; no `internal` health route forwarded. Config parsing: `host_write_key` missing leaves the route disabled, equal to `host_key` is a `ValueError` naming the key but not its value; rendered slice contains the digest and never the key.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** nginx, compose (no new ports or networks; relay stays one-way), config render.
- [ ] **Step 4: Run** the compose test file and the fleet-config tests one at a time; if the existing docker smoke runs there, run it and assert from inside the relay that `POST /v1/items/x/versions/y/shares` reaches the host (401 without key) and `POST /v1/feed` is 403.
- [ ] **Step 5: Commit** `feat(deploy): relay allows exactly one POST route for share creation`.

### Task 5: cp-web signed content URL, mint and prefix route

**Files:**
- Create: `herdr_engineering/control_plane/library_content_token.py`, `herdr_engineering/control_plane/artifact_library_content_api.py`
- Modify: `herdr_engineering/control_plane/artifact_host_client.py` (`csp_sandboxes` -> `csp_is_dashboard_content`, config key), `herdr_engineering/control_plane/artifact_library_api.py` (old content proxy removed), `herdr_engineering/control_plane/api.py` (wire the new prefix before auth in `do_GET`; secret loaded at startup)
- Test: `tests/control_plane/test_library_content_token.py`, `tests/control_plane/test_artifact_library_content_api.py`, `tests/control_plane/test_artifact_host_client.py`, `tests/control_plane/test_artifact_library_api.py` (update 502 test at :421)

**Interfaces:**
- Produces:
  - `sign(secret: bytes, item: str, version: str, expires_at: int) -> str` returns `<expires>.<b64url(hmac_sha256(secret, "v1\0item\0version\0expires"))>`; `verify(secret: bytes, token: str, item: str, version: str, now: int) -> bool` (constant-time, rejects malformed, expired).
  - Route `GET /api/v1/library/items/<id>/view` (operator role, normal auth): returns `{"base": "/library-content/<token>/<item>/<version>/", "expires_at": int, "entry": "<name>"}`; TTL 300 s; secret is 32 random bytes generated at startup or from `control_plane.artifact_library.content_secret` (rotation = restart).
  - Prefix route `GET /library-content/<token>/<item>/<version>/<path...>`: NOT under `/api/`, handled before `_role()`; verifies the token against the item and version in the path, then proxies via `ArtifactHostClient.open_content`. Forwards the host CSP byte for byte, sets `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, no cookie, and `Access-Control-Allow-Origin` absent. Failures are 404 with one identical body (never 401/403, so existence of tokens/items does not leak).
  - `csp_is_dashboard_content(policy) -> bool`: true only when the sandbox directive is exactly `allow-scripts allow-downloads`, the policy has `default-src` containing `'self'`, `frame-ancestors 'self'`, `object-src 'none'`, and equals the host's `dashboard_content_csp()` string (pinned by a drift test that imports both sides in the test only).

- [ ] **Step 1: Write failing tests (real HTTP, real host fixture as `test_artifact_library_api.py` does):**
  - Token unit tests: round trip; tampered signature/expiry/item/version each fail; expired fails; empty, oversize, non-ASCII, missing parts fail; two different secrets do not verify each other.
  - `test_valid_url_serves_the_bundle_and_relative_assets`: index.html, a CSS file and a nested `img/a.png` all load via relative paths under the prefix.
  - Negative suite, each asserting the identical 404 body and that the host received no content request: tampered token, expired, wrong item, wrong version (token for v1 on v2), `..`, `%2e%2e`, `%2f`, `%252e%252e`, `..%5c`, NUL, empty path segment, path > limit.
  - `test_the_exemption_reaches_no_other_route`: with no API key, `/library-content` (no trailing parts), `/library-content-x/...`, `/api/v1/library`, `/api/v1/library/items/<id>`, `/api/v1/library/items/<id>/view`, `/api/v1/ci...` all stay 401 when keys are configured; POST/PUT/DELETE to a valid content URL is 405.
  - `test_mint_requires_operator_and_returns_no_host_key`: viewer/no key -> 401/403; response and every header contain neither `host_key`, `host_write_key` nor `content_secret`.
  - `test_a_host_response_with_any_other_policy_is_502` (replaces :421): script-free old `CONTENT_CSP`, the public `/s/` CSP verbatim (frame-ancestors 'none'), policy with extra `allow-same-origin`, extra `allow-forms`, `allow-popups`, missing nosniff, missing type, all 502.
  - `test_expired_url_gives_404_then_remint_works`.
- [ ] **Step 2: Run** each file. Expected: FAIL.
- [ ] **Step 3: Implement.** Mint verifies the item/version exists through `client.item`/`client.versions` (404 passes through) and that the version is `ready`; entry chosen as the manifest's primary file via the same rule as the share `choose_entry`.
- [ ] **Step 4: Run** each file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(control-plane): signed short-lived library content URLs`.

### Task 6: cp-web create-share client and route

**Files:**
- Modify: `herdr_engineering/control_plane/artifact_host_client.py`, `artifact_library_api.py`, `LibraryHostConfig` (`host_write_key`)
- Test: `tests/control_plane/test_artifact_host_client.py`, `tests/control_plane/test_artifact_library_api.py` (read-only 405 test widens)

**Interfaces:**
- Produces:
  - `ArtifactHostClient.create_share(item: str, version: str, *, expires_in_days: int | None, operator: str) -> dict[str, Any]` (POST, write key, `X-Herdr-Operator`, MessagePack body; raises `HostRejected(status, code)`; `HostUnavailable` otherwise). Constructed only when `host_write_key` is configured.
  - Route `POST /api/v1/library/items/<id>/versions/<vid>/shares` (operator role) body `{"expires_in_days"?: int}`; response 201 `{"share_id","url","expires_at","token"}` (token only here). `operator` = the request principal id from `_principal()` (`key:<12-hex digest>`, or `open-listener`); never taken from the request body or a header the browser sends.
  - Every other non-GET under `/api/v1/library` stays 405 `Allow: GET`; the share route answers 405 `Allow: POST` for other methods. Not configured -> 503 `sharing_unavailable`.

- [ ] **Step 1: Write failing tests:** against a real host: create returns a working public link whose `/s/<token>/` page loads on the public listener; owner member listing shows it; a client-supplied `operator` body field or `X-Herdr-Operator` header is ignored (event actor is the principal); read-only role -> 403; every other library write (`POST /api/v1/library`, `DELETE`, `PUT`, `PATCH`, POST to `/folders`, `/shares`, `/items/<id>`) is still 405 `Allow: GET`; host refusals (403 `member_removed`, 409 `version_not_ready`, 404) pass through with their codes; the host write key never appears in any response; a client built without the write key refuses locally.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(control-plane): dashboard create-share route`.

### Task 7: Frame module and no-frames pin

**Files:**
- Modify: `packages/library-frame.js`, `packages/library-frame.test.js`, `tests/control_plane/test_no_frames.py` (`library_frame_shape_problems`, `ALLOWED` reason text)

**Interfaces:**
- Produces: `isContentUrl(url: string): boolean` true only for `^/library-content/[A-Za-z0-9._-]+/[^/?#]+/[^/?#]+/[^?#]+$` with no `.`/`..` decoded segment; `mountLibraryFrame(slot, { url, title, document })` sets `sandbox="allow-scripts allow-downloads"` (exactly this token list, in this order), `src`, `referrerpolicy="no-referrer"`, no `allow`, no `srcdoc`, and returns the frame; also exports `frameContainer(slot)` used by fullscreen.

- [ ] **Step 1: Write failing tests:** JS: only the prefix shape is accepted (old `/api/v1/library/content/...`, absolute URLs, `//host`, `javascript:`, `data:`, traversal all throw); attribute exactly as above. Python: the shape checker accepts exactly that sandbox value and rejects `""`, `allow-same-origin`, extra tokens, `allow-top-navigation`, `srcdoc`, a second iframe, and any other file creating frames.
- [ ] **Step 2: Run** `node --test packages/library-frame.test.js` and `pytest tests/control_plane/test_no_frames.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run both.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): library frame runs scripts in an opaque-origin sandbox`.

### Task 8: Item page: top bar, version picker, drawer, download

**Files:**
- Modify: `packages/library-view.js` (`renderLibraryItem`, `mountLibraryItem`), `packages/herdr-web-client/client.js` (`libraryView(id, version)`, `libraryCreateShare(id, version, body)`), styles in the existing library stylesheet
- Test: `packages/library-view.test.js`, `packages/herdr-web-client/library-client.test.js`

**Interfaces:**
- Consumes: Task 5 `GET .../items/<id>/view`, Task 6 create-share route, Task 7 `mountLibraryFrame`.
- Produces: `renderLibraryItem(state) -> string` with, in order: a `<header data-library-bar>` holding `a[data-action=back]` (`#/library`), `h2` title, `select[data-action=version]` (latest first, default = latest ready), `button[data-action=share]`, `a[data-action=open-public]` (rendered `aria-disabled="true"` with no `href` unless a live unexpired share exists), `a[data-action=download][download]` (`libraryContentUrl` of the version's primary file via the existing GET content route, no new route), `button[data-action=fullscreen]`; then `[data-frame-slot]` (the artifact, mounted automatically on load); then `<aside data-library-drawer hidden>` (owner, folders, topics, share list, version history) toggled by `button[data-action=drawer]`. `mountLibraryItem` re-mints and reloads the frame quietly at `expires_at - 30 s` and on frame load failure (one retry, then an inline "Reload" button). Changing the picker updates `?version=` and the frame.

- [ ] **Step 1: Write failing tests (node:test, DOM from the existing test helpers):** frame is mounted before the drawer in DOM order; drawer closed by default and toggles `hidden`/`aria-expanded`; picker lists versions newest first, default latest; open-public disabled without a live share, enabled with `href` of the live share's public page when one exists, expired and revoked shares do not enable it; download href points at `/api/v1/library/content/<item>/<version>/<primary>`; no token appears anywhere in the rendered markup; re-mint timer calls the client again and replaces `src`; no old "Open a file to preview" text remains.
- [ ] **Step 2: Run** `node --test packages/library-view.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.** Follow `library-view.js` conventions (`esc`, `when`, light tokens only). Remove the old file-list preview button path.
- [ ] **Step 4: Run** both JS test files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): frame-first library item page with top bar and drawer`.

### Task 9: Share dialog and fullscreen

**Files:**
- Create: `packages/library-share-dialog.js`, `packages/library-fullscreen.js` and their `.test.js`
- Modify: `packages/library-view.js` (wire actions)

**Interfaces:**
- Produces:
  - `openShareDialog({ container, document, create, clipboard }) -> Promise<{ url } | null>`: `<dialog>` with a number input (default 30, min 1, max 90, integer), Create and Cancel; on success shows the link, copies it (`clipboard.writeText`; failure leaves the link selectable and shows "Copy failed, select the link"), never stores it in `localStorage`/`sessionStorage`/URL; closing clears the link from the DOM. Out-of-range input disables Create. After success the page refreshes shares so open-public enables.
  - `toggleFullscreen(container: Element, doc: Document = document): Promise<'entered' | 'exited' | 'unavailable'>`: requests fullscreen on the frame container (never on the iframe's document); when `document.fullscreenEnabled` is false it sets `data-expanded` on the container (CSS: fixed, inset 0, z-index above bar) and Escape or the button restores it.

- [ ] **Step 1: Write failing tests:** expiry validation (0, 91, 1.5, empty); default 30 sent as `expires_in_days: 30`; link shown once and removed on close; clipboard rejection path; fullscreen enters/exits on the container (assert `requestFullscreen` target is the container element), fallback path toggles `data-expanded` and Escape restores; `sandbox` attribute unchanged after both.
- [ ] **Step 2: Run** each `.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(web): share dialog and fullscreen for the library viewer`.

### Task 10: Playwright E2E against a real host

**Files:**
- Modify: `tests/playwright/library-dashboard-server.py` (configure `host_write_key`, seed an artifact whose page runs a script, loads a relative CSS and image, and attempts to reach the parent), `tests/playwright/library-dashboard.test.mjs`

**Interfaces:**
- Consumes: everything above. The seeded page, on load, writes into its own DOM: `SCRIPT RAN`, the result of `try { parent.document.title } catch (e) { e.name }` as `PARENT:<name>`, `document.cookie`/`localStorage` access result, and `fetch('/api/v1/library')` status or error as `FETCH:<result>`.

- [ ] **Step 1: Write failing E2E tests** (`node --test tests/playwright/library-dashboard.test.mjs`):
  - `opening an artifact shows it running first`: after navigating to `#/library/<id>` the frame has `sandbox="allow-scripts allow-downloads"` (no `allow-same-origin`), the frame text contains `SCRIPT RAN`, the relative CSS is applied (computed style) and the image loaded; the drawer is closed; no token or `library-content` URL text is visible in the page text.
  - `the frame cannot read the parent`: frame shows `PARENT:SecurityError`, storage/cookie access throws, `fetch('/api/v1/library')` shows a failure or a 404/401 never a feed body; `window.name`/title of the dashboard unchanged.
  - `top bar`: back goes to `#/library`; version picker switches to an older version and the frame content changes; open-public is disabled before a share exists.
  - `share creation`: dialog default 30, enter 91 -> Create disabled, enter 7 -> link shown and copied (`page.evaluate(navigator.clipboard.readText)` with permission granted); open-public becomes enabled; the public listener serves the link; the owner's member API lists the share as live; the link is gone after closing the dialog; reloading the page shows no token.
  - `fullscreen`: clicking the button makes `document.fullscreenElement` the frame container (not the iframe), Esc exits, frame `sandbox` unchanged and script still runs.
  - `expired content URL reloads quietly`: server option sets TTL to 2 s; after 3 s the frame still shows content without a visible error.
  - `an unknown or tampered content URL 404s in a plain tab`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** server seed changes only; no product code except fixes found by the tests (each fix gets its own unit test in the owning task's file).
- [ ] **Step 4: Run** `node --test tests/playwright/library-dashboard.test.mjs`, then `node --test tests/playwright/*.test.mjs`. Expected: PASS.
- [ ] **Step 5: Commit** `test(e2e): dashboard artifact viewer`.

### Task 11: Opus 5.5 security review

**Files:** none (review output goes to the PR).

- [ ] **Step 1: Ask the owner for explicit approval** to run an Opus 5.5 subagent (standing rule), then dispatch it with the diff of this branch since the end of host Task 12, the spec, and this plan's Review Focus.
- [ ] **Step 2: Reviewer must try to break** each of: frame escape to dashboard origin; signed-URL forgery/replay/traversal; prefix-exemption leakage; read/write key confusion; token leakage (logs, list responses, error bodies, `Referer`, history); relay widening beyond one POST; CSP drift between host and proxy; fullscreen/`allow-downloads` abuse; operator-actor spoofing; removed/retracted/purged item sharing.
- [ ] **Step 3: Fix every finding** with a failing test first in the owning task's file; re-run the affected files and the Playwright suite.
- [ ] **Step 4: Run** `/code-review` on the final diff; merge via PR with CI green.

---

## Self-Review

- Coverage: write key + one POST (T1-2), relay (T4), actor event (T1-2, 6), refusals (T1), expiry (T1, 9), token once (T2, 6, 9, 10), signed URL + exempt prefix + negatives (T5), internal public sandbox CSP and proxy shape, old path removed (T3, T5), item page, bar, drawer, picker, share dialog, open-public disabled, download, fullscreen (T8-9), E2E (T10), Opus review (T11).
- Types: `create_operator_share` (T1) -> route (T2) -> `ArtifactHostClient.create_share` (T6) -> `libraryCreateShare` (T8); `dashboard_content_csp` (T3) <-> `csp_is_dashboard_content` (T5); `isContentUrl` prefix (T7) <-> minted `base` (T5).
- Spec conflicts and calls: see the report accompanying this plan (frame-ancestors, WebKit `'self'`, "session auth" wording, operator identity, DB write grants, API-response CSP).
