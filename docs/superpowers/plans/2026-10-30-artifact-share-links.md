# Artifact share links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator share one pinned artifact version as a frozen file bundle with the lab Cognito pool (named people/groups) or with anyone holding a link, served from a database-free public container.

**Architecture:** Owner API in control-plane-web copies a version's output files into a read-only volume at approve time and writes a per-token `state.json` projection; a new minimal `share-public` server (same image) serves `/s/<token>/...` from that volume with Range, CORS and sandbox CSP; lab links sign in with Cognito (PKCE in a static viewer page), exchange the verified tokens for a stateless HMAC session path. Audit events are appended to a file and ingested by the worker.

**Tech Stack:** Python stdlib `http.server` (like the existing handlers), PyJWT via `CognitoAuthenticator`, Postgres migrations, Docker compose + nginx, vanilla JS (PKCE) for the viewer, Playwright for the browser checks.

**Spec:** `docs/superpowers/specs/2026-10-30-artifact-share-links-design.md` (revision 2, approved by the operator 2026-10-30).

## Global Constraints

* Caps (YAML `control_plane.share`): 512 MiB per share, 20,000 files per share, 2 GiB across live shares. Defaults only; values come from YAML, never env vars.
* Public shares ALWAYS expire: default 30 days, allowed 1–90. Lab shares: expiry optional, none by default.
* Token = `secrets.token_urlsafe(32)`, shown ONCE; only `sha256(token)` hex is stored/used as a path. Tokens never appear in logs, events or API list output.
* Every public failure (unknown/revoked/expired, wrong user, bad session, missing file, traversal) returns the SAME 404 body; reasons go only to `events.jsonl`.
* Content headers on EVERY non-PDF response: `Content-Security-Policy: sandbox allow-scripts allow-downloads; default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'`, plus `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-cache`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`, ETag = `"<sha256>"`.
* PDFs: `Content-Type: application/pdf`, `Content-Disposition: inline`, `nosniff`, only `frame-ancestors 'none'`.
* CORS on EVERY response (200/206/304/404): `Access-Control-Allow-Origin: *`, `Access-Control-Expose-Headers: Content-Range, Content-Length, Accept-Ranges, ETag`; `OPTIONS` → 204 with `Access-Control-Allow-Methods: GET, HEAD, OPTIONS`, `Access-Control-Allow-Headers: Range, If-Range, If-None-Match, If-Match`, `Access-Control-Max-Age: 600`. Methods allowed: GET, HEAD, OPTIONS only.
* Lab sessions: path `/s/<token>/k/<session>/f/<file>`; `session_seconds` default 14400 but NEVER beyond the share's expiry or the verified access token's `exp`. (Deviation from spec: the spec checked groups against `members.json`, which only syncs the one configured group; group allow-lists therefore use the verified token's `cognito:groups` claim, which is why the session is capped at the token's lifetime.) Named emails are authorised by the verified id-token email only.
* `herdr-share-public` has NO database, RustFS, AWS or control-plane network; only these mounts: `/shares` (ro), `/share-events` (rw, events file only), the Cognito sync dir (ro), its own config slice (ro).
* Migration is the next free number after `0035` at implementation time (re-check `deploy/control-plane/migrations/`), `IF NOT EXISTS`, `-- DOWN:` comment.
* Commits end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. No secrets, passwords or tokens in the repo; YAML references only. No `--delete-branch` or squash on stacked PRs. Production steps (migration, rollout, Cognito callback, edge reload) happen only with the operator's go-ahead at that moment.

## Review Focus

* Path traversal and odd encodings in `/s/<token>/f/<path>` (`..`, `%2e%2e`, `//`, backslash, NUL, very long, case-fold duplicates, unicode): never serves outside the bundle, always the uniform 404.
* Range edge cases for parquet readers: suffix `bytes=-N`, open-ended, past EOF (416), multi-range, mismatched `If-Range`, HEAD, zero-byte file.
* Revoke/expiry while a lab session is live: the very next request 404s; a session can never outlive the share or the token.
* Lab identity mix-ups: user in the pool but not on the list, unverified email, id token of a different user than the access token, access token from a non-allow-listed client.
* Hostile or odd bundles: external CDN references (warned), nested names, case-fold duplicates, symlink-like names, oversize/too many files, entries lacking size/media_type or hash.

## File structure

* Create `herdr_engineering/control_plane/share_bundle.py` — name rules, content types, bundle writing, state projection, reference scan (pure, no HTTP/DB).
* Create `herdr_engineering/control_plane/artifact_shares.py` — `ShareStore` (SQL) + token helpers + events ingest.
* Create `herdr_engineering/control_plane/artifact_share_api.py` — owner routes + approve pipeline; hook one line into `api.py` next to `route_report`.
* Create `herdr_engineering/control_plane/share_public.py` — the database-free server (`ShareConfig`, `make_share_server`).
* Create `herdr_engineering/control_plane/share_session.py` — HMAC session mint/check; `share_lab.py` — lab authorisation.
* Create `herdr_engineering/control_plane/share_static/{viewer.html,share.js,share.css}` — PKCE viewer.
* Create `herdr_engineering/artifact_shares_client.py` — CLI `artifacts share ...`; register beside `add_report_subparser` (`cli.py:302`).
* Modify `herdr_engineering/cli.py` (`control-plane share-public` command), `fleet_config_cli.py` (`render_share`), `deploy/control-plane/{compose.yaml,compose.collab.yaml,deploy.sh,collab-edge/nginx.conf,fleet-config.example.yaml,COLLAB.md}`, worker loop (`worker.py`) for sweep+ingest.
* Create `deploy/control-plane/migrations/0041_artifact_shares.sql` (renumber if taken), tests under `tests/control_plane/test_share_*.py`, `tests/test_artifact_shares_client.py`, `scripts/share-browser-check.mjs` + `tests/share-fixtures/`, skill docs in `~/IdeaProjects/herdr-artifacts` (separate repo/PR).

---

### Task 1: Bundle rules and projection (`share_bundle.py`)

**Files:** Create `herdr_engineering/control_plane/share_bundle.py`; Test `tests/control_plane/test_share_bundle.py`

**Interfaces:**
* Produces: `class ShareBundleError(ValueError)`; `normalise_name(name: str) -> str`; `content_type_for(name: str) -> str`; `@dataclass BundleLimits(max_bytes: int, max_files: int)`; `@dataclass BundleFile(name, sha256, size, content_type)`; `@dataclass BundleIndex(entry: str, files: list[BundleFile], total_bytes: int, warnings: list[str])`; `build_bundle(dest: Path, files: Iterable[tuple[str, str, int, Iterable[bytes]]], *, entry: str | None, limits: BundleLimits) -> BundleIndex` (each tuple is name, expected sha256, expected size, chunks; verifies digest/size, writes under `dest/files/`); `scan_external_references(html: bytes) -> list[str]`; `write_state(root: Path, token_hash: str, state: dict) -> None` (atomic rename into `root/by-token/<token_hash>/state.json`, 0644 dirs 0755); `remove_state(root, token_hash)`; `remove_bundle(root, share_id)`.

- [ ] **Step 1: Write failing tests** — `test_normalise_rejects_escape_forms` (`../x`, `/abs`, `a//b`, `a\\b`, NUL, 300-char segment, `.`/`..` segments → `ShareBundleError`; `data/x.parquet` kept), `test_content_type_table` (`.parquet` → `application/vnd.apache.parquet`; `.wasm` → `application/wasm`; `.svg` → `image/svg+xml`; `.pdf` → `application/pdf`; unknown → `application/octet-stream`), `test_build_bundle_verifies_hash_size_and_caps` (wrong sha, short/long body, over `max_bytes`, over `max_files`, case-fold duplicate `A.html`/`a.html`, zero-byte file allowed), `test_entry_selection` (explicit `entry`; else `index.html`; else the single html; else the single pdf; else error naming the choices), `test_scan_external_references` (`<script src="https://cdn…">`, `url(//x)`, `import 'https://…'` are reported; relative and `data:` are not), `test_state_written_atomically_and_removed`.
- [ ] **Step 2: Run to verify failure** — `pytest tests/control_plane/test_share_bundle.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** the interface above; extension→type table is the allow-list from the spec (html, css, js/mjs, json, csv, tsv, txt, svg, png/jpg/gif/webp, pdf, parquet, wasm, woff2). Write files with `O_EXCL`, never follow symlinks, and delete a partially written `dest` on any error.
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** `feat(shares): bundle rules and state projection`.

### Task 2: Migration and `ShareStore`

**Files:** Create `deploy/control-plane/migrations/0041_artifact_shares.sql`, `herdr_engineering/control_plane/artifact_shares.py`; Test `tests/control_plane/test_share_store.py` (Postgres fixtures as in `test_artifact_report_postgres.py`)

**Interfaces:**
* Consumes: `BundleIndex` (Task 1).
* Produces: `new_token() -> tuple[str, str]` (token, sha256 hex); tables `artifact_shares`, `artifact_share_events` exactly as in the spec; `class ShareStore` built the way `ArtifactStore` is (follow `artifact_adapter.py` for the pool/`sync_call` pattern) with `create(*, artifact_id, version_id, audience, allow_emails, allow_groups, expires_at, entry, title, created_by) -> dict`, `get(share_id) -> dict | None`, `list_for_artifact(artifact_id) -> list[dict]` (no token_hash), `mark_live(share_id, *, token_hash, index: BundleIndex, approved_by) -> dict`, `mark_revoked(share_id, *, actor) -> dict`, `live_expired(now) -> list[dict]`, `live_total_bytes() -> int`, `add_event(share_id, kind, actor, detail)`, `ingest_events_file(path: Path) -> int` (reads complete JSON lines, appends, truncates only what it consumed, tolerates a torn last line), `events(share_id, limit=200) -> list[dict]`.

- [ ] **Step 1: Tests** — `test_create_pending_validates_audience_rules` (lab needs email or group; public refuses allow-lists; public requires `expires_at` within 1–90 days; lab expiry optional), `test_state_machine` (pending→live→revoked; revoke twice is a no-op; approve a revoked/expired share refused), `test_token_hash_unique_and_never_listed`, `test_ingest_events_file_is_idempotent_and_tolerates_torn_line`, `test_migration_up_and_down_names`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** migration + store. **Step 4: Run** → PASS. **Step 5: Commit** `feat(shares): migration 0036 and ShareStore`.

### Task 3: Owner API and approve pipeline

**Files:** Create `herdr_engineering/control_plane/artifact_share_api.py`; Modify `api.py` (one dispatch line beside `route_report`, plus `server.share_store`, `server.shares_root`, `server.share_settings` constructor args wired from `cli.py`); Test `tests/control_plane/test_artifact_share_api.py` (pattern: `reports_server` fixture in `test_artifact_reports_api.py`)

**Interfaces:**
* Consumes: `ShareStore`, `build_bundle`, `write_state`, `render_report_preview`, `server.rustfs.iter_get(storage_key, sha256, chunk_size=)`, `handler._access(artifact_id, "share")`.
* Produces: routes `POST /api/v1/artifacts/{id}/shares`, `POST .../shares/{sid}/approve`, `GET .../shares`, `POST .../shares/{sid}/revoke`, `GET .../shares/{sid}/events`; `def route_share(handler, server, parts, resource) -> bool`; `def approve_share(server, share: dict) -> tuple[str, dict]` returning `(url, summary)` where summary has entry, file_count, total_bytes, expires_at, audience, warnings.

- [ ] **Step 1: Tests** — `test_create_requires_share_permission_and_is_pending_without_token`, `test_create_refuses_unready_version_and_missing_html_or_pdf`, `test_approve_requires_confirm_true_and_returns_url_once` (second approve → 409; list never shows token), `test_approve_copies_nested_outputs_and_pins_content` (later registry change does not alter served bytes), `test_approve_refuses_entries_without_hash_and_measures_missing_size`, `test_report_version_gets_generated_preview_html_entry`, `test_caps_enforced_including_total_live_bytes`, `test_blob_not_owned_by_artifact_is_refused` (reuse the scoped-blob check), `test_revoke_removes_state_and_bundle_and_is_idempotent`, `test_cross_origin_post_refused`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** — mirror `route_report` structure (`_body` allow-list, `_access`, 404 on denial); approve enumerates `version["output_manifest"]` the way the `outputs/{n}` route validates digests (reuse its checks, factor a shared helper rather than copying), streams each output through `iter_get` into `build_bundle`, generates `preview.html` for report versions, writes `state.json` last (so a half-built share is never live). **Step 4:** PASS. **Step 5:** Commit `feat(shares): owner API and approve pipeline`.

### Task 4: Worker sweep and events ingest

**Files:** Modify `herdr_engineering/control_plane/worker.py`; Test `tests/control_plane/test_share_worker.py`

**Interfaces:** Produces `async def share_housekeeping(conn_or_store, shares_root: Path, events_path: Path, now: float) -> dict` returning `{"expired": n, "ingested": n}`; called once a minute from the worker loop only when `control_plane.share.enabled`.

- [ ] **Step 1: Tests** — expired live share → state removed, bundle removed, row `revoked`-equivalent `expired` event; events file ingested then truncated; disabled config does nothing.
- [ ] Steps 2–5 as above. Commit `feat(shares): expiry sweep and audit ingest`.

### Task 5: The public file server (`share_public.py`)

**Files:** Create `herdr_engineering/control_plane/share_public.py`; Test `tests/control_plane/test_share_public.py`

**Interfaces:**
* Consumes: state layout from Task 1.
* Produces: `@dataclass ShareConfig(shares_root: Path, events_path: Path, sync_dir: Path | None, pool_id, client_ids, web_client_id, cognito_domain, public_url, signing_secret: bytes, session_seconds: int, static_dir: Path)`; `def make_share_server(config: ShareConfig, *, host: str, port: int, now=time.time) -> ThreadingHTTPServer`; `def load_state(root: Path, token: str, now: float) -> dict | None` (None unless `state == "live"` and unexpired; token must match `[A-Za-z0-9_-]{43}`).

- [ ] **Step 1: Tests** (real sockets, tmp bundles) — `test_public_share_serves_entry_redirect_and_files`, `test_every_failure_is_the_same_404_body` (unknown token, revoked, expired, traversal list from Review Focus, bad method), `test_range_matrix` (suffix, open-ended, past EOF 416, multi-range, mismatched If-Range, HEAD, zero-byte), `test_headers_exact` (non-PDF CSP string verbatim; PDF headers; SVG gets the sandbox CSP; CORS on 200/206/304/404; OPTIONS preflight), `test_etag_304`, `test_revoke_takes_effect_on_next_request` (delete state.json between calls), `test_events_appended_not_tokens` (events contain share_id and kind, never the token).
- [ ] **Step 2:** FAIL. **Step 3: Implement** a small handler class (stdlib), streaming files in 1 MiB chunks, `Content-Length` correct for 206, no directory listings, path decoded once then normalised by `normalise_name`, files opened with `O_NOFOLLOW`; events written with one `write` per line under `O_APPEND`, size-capped (drop and count when over cap). **Step 4:** PASS. **Step 5:** Commit `feat(shares): database-free public share server`.

### Task 6: Lab sign-in, sessions and viewer

**Files:** Create `share_session.py`, `share_lab.py`, `share_static/{viewer.html,share.js,share.css}`; Modify `share_public.py` (lab routes); Test `tests/control_plane/test_share_lab.py`

**Interfaces:**
* Produces: `mint_session(secret: bytes, share_id: str, identity: str, exp: float) -> str`; `check_session(secret: bytes, share_id: str, session: str, now: float) -> dict | None` (constant-time compare; returns identity, exp); `authorise_lab(state: dict, access_token: str, id_token: str, authenticator: CognitoAuthenticator) -> dict | None` returning `{identity, email, exp}` or None. Authenticator is built with `open_signup=True` (pool tokens only) and the allow-list is applied here: verified id-token email in `allow_emails` OR any token group in `allow_groups`; id token must belong to the same user as the access token (use `verify_id_token(..., user=)`).
* Routes added to the server: `GET /s/<token>/` (lab → viewer page; public → redirect), `GET /s/static/{share.js,share.css}`, `GET /s/callback`, `POST /s/<token>/session` (headers `Authorization: Bearer`, `X-Id-Token`; body-less) → `{"base": "/s/<token>/k/<session>/f/", "entry": "...", "expires_at": ...}`, files under `/s/<token>/k/<session>/f/<path>`.

- [ ] **Step 1: Tests** (fake JWKS pattern from `test_collab_cognito.py`) — each Review Focus identity case (not on list, unverified email, mismatched user, wrong client), group-claim allow, email allow without any group, session HMAC tamper/expiry/other-share, session capped at min(share expiry, token exp, `session_seconds`), revoke kills a live session, session endpoint is POST-only and never echoes tokens, viewer page and JS are served with a strict CSP (`script-src 'self'`, no inline).
- [ ] **Step 2:** FAIL. **Step 3: Implement.** `share.js`: PKCE (S256) code flow against `cognito_domain` with `redirect_uri = <public_url>s/callback`, `state` carrying the share path, tokens only in `sessionStorage`, `fetch` session endpoint, then `location.replace(base + entry)`; show a neutral "not available" message on any failure. **Step 4:** PASS. **Step 5:** Commit `feat(shares): lab sign-in, stateless sessions, viewer`.

### Task 7: CLI and skill docs

**Files:** Create `herdr_engineering/artifact_shares_client.py`; Modify `cli.py` (register next to `add_report_subparser`, line ~302); Test `tests/test_artifact_shares_client.py`; skill docs (herdr-artifacts repo): `skill/herdr-artifact/references/share-commands.md` and the safety-contract paragraph in `SKILL.md`.

**Interfaces:** `herdr-eng artifacts share {create,approve,list,events,revoke}`; `create A V --audience lab|public [--email E ...] [--group G ...] [--expires-in-days N] [--entry PATH] [--title T]`; `approve A SHARE_ID` prints the exposure summary (entry, files, size, expiry, audience, warnings) and requires `--yes` (non-interactive) or an interactive "yes"; prints the URL once; `ReportClient`-style helper reused for HTTP/`--control-plane`/`--api-key-env`.

- [ ] Steps: tests (argument validation, summary printing, no token in `list`/`events` output, `--yes` required when stdin is not a TTY) → fail → implement → pass → docs → commit `feat(shares): CLI and skill reference`.

### Task 8: Deploy wiring (compose, edge, fleet slice)

**Files:** Modify `compose.yaml` (mount `${HERDR_SHARES_DIR}:/shares` rw on control-plane-web and worker), `compose.collab.yaml` (new service `share-public`, edge `depends_on`, network), `deploy.sh` (create dirs, render slice, export `HERDR_SHARES_DIR`, `HERDR_SHARE_EVENTS_DIR`, `HERDR_SHARE_CONFIG_DIR`), `collab-edge/nginx.conf` renderer, `fleet_config_cli.py` (`render_share`), `cli.py` (`control-plane share-public` subcommand and `--share-*` wiring for web/worker), `fleet-config.example.yaml`, `COLLAB.md`; Test `tests/test_share_deploy_config.py`

**Interfaces:** `def render_share(cfg: FleetConfig, out: Path) -> None` writes a 0600 slice holding ONLY `control_plane.collab.{cognito_pool, cognito_clients, cognito_web_client, cognito_domain, public_url}` and `control_plane.share.{enabled, max_*, default_public_days, session_seconds, signing_secret}`; `share-public` is on a new internal network `share` (edge joins it too), `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, no `env_file`; nginx: `location ^~ /s/` → `proxy_pass http://share-public:8083`, GET/HEAD/OPTIONS only, own `limit_req_zone`, `proxy_buffering off`, `proxy_read_timeout 600s`, `client_max_body_size 0`→ requests with bodies rejected, `access_log off`.

- [ ] Steps: tests first — (a) rendered nginx contains the `/s/` location and no other new location, (b) slice contains no DSN/API keys/other sections, (c) compose lint: `share-public` has no postgres/rustfs network, only the four mounts, and the control-plane-web `command` list still equals `compose.yaml`'s (existing "kept in step" test), (d) `deploy.sh --dry-run` accepts `share.enabled: false` unchanged and refuses `enabled: true` without `signing_secret` with an instruction (generate locally, store in the YAML; never print). Implement, run `tests/test_share_deploy_config.py` and the existing compose/deploy tests, commit `feat(shares): compose, edge and fleet slice wiring`.

### Task 9: Browser verification harness

**Files:** Create `scripts/share-browser-check.mjs`, `tests/share-fixtures/{pdf-only,html-parquet-hyparquet,html-parquet-duckdb}/` (self-contained bundles with vendored libs; document how they were produced), README section.

**Interfaces:** `node scripts/share-browser-check.mjs --browser chromium|firefox|webkit` starts `make_share_server` over a temp shares root (public mode and a lab mode against a fake Cognito issuer), loads each bundle in the browser, asserts: HTML renders, the parquet table appears (range requests observed server-side, including a suffix range), a script cannot read the host origin's `sessionStorage` (assert `SecurityError`), an external CDN URL is blocked, PDFs display (screenshot), revoked link shows the neutral page.

- [ ] Steps: write the harness and fixtures, run it in all three browsers, record the results in the PR body. A failing library = stop and report to the operator (spec: no safe in-design fix). Commit `test(shares): browser verification harness`.

### Task 10: Rollout and live smoke test (operator go-ahead per step)

- [ ] Open the PR, make CI green (restart flaky `ui-gates` per the known baseline issue only after confirming the failure is unrelated), merge with a merge commit.
- [ ] With the operator's go-ahead: generate `signing_secret` locally into the fleet YAML (not printed), run `deploy/control-plane/deploy.sh --ref origin/main --no-fetch` from a clean worktree (applies migration `0036`, creates volumes, recreates web/worker/llm-proxy, starts `share-public`, regenerates and reloads the edge).
- [ ] With the operator's go-ahead: add `https://localdev.mcp.metabolomics.us/s/callback` to `CallbackURLs` and `LogoutURLs` of `herdr-collab-web` (`aws cognito-idp update-user-pool-client`, preserving every other attribute — read the client first and send the full set).
- [ ] Smoke test through the real edge: public PDF; public HTML+parquet bundle (suffix range seen in `docker logs`-free events file); lab link with the operator's own account (named email); a pool user NOT on the list is refused; revoke → 404 immediately; expiry sweep. Repeat the three-browser check against the live URL. Report results and the operator's real artifacts' outcome before sending any link to others.
- [ ] Update the memory note and docs; offer the operator the HerdR file-viewing of generated screenshots.

---

## Self-review

* **Spec coverage:** audiences/states (T2,T3), bundle + caps + warnings (T1,T3), file projection with no DB in the public container (T1,T5,T8), Range/CORS/CSP (T5), lab login + sessions (T6), audit trail + expiry (T4,T5), CLI/skill (T7), edge/compose/slice (T8), browser/sandbox risk test (T9), rollout order and Cognito callback (T10). Spec text about `members.json` group freshness is replaced by the documented token-claim deviation (Global Constraints).
* **Type consistency:** `BundleIndex`, `new_token`, `ShareStore` methods, `load_state`, `mint_session/check_session`, `authorise_lab` names are used identically in the tasks that consume them.
* **Proportion:** each task names signatures, tests and spec values; bodies are left to the implementer.
