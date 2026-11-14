# Artifact Host Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract member artifacts, the artifact library and share serving into a standalone `artifact-host` app (own image, containers, Postgres, blob volume) that the private dashboard reads read-only.

**Architecture:** New top-level package `herdr_artifact_host/` that imports nothing from `herdr_engineering`. One image, two roles: `public` (edge-facing: member page/API, `/s/`, `/mcp/artifacts`) and `internal` (GET-only API for the dashboard, network `artifact-internal`). Own Postgres and a content-addressed blob volume. The dashboard `/library` becomes a client of the internal API; the control plane keeps no member data.

**Tech Stack:** Python 3.13, asyncpg + PostgreSQL 16, stdlib HTTP server pattern already used by `share_public.py`, nginx edge, docker compose, pytest with real Postgres (testcontainers), Playwright via `node --test tests/playwright/*.test.mjs`.

**Spec:** `docs/superpowers/specs/2026-11-06-artifact-host-app-design.md` (binding). Coupling map for reference: `.superpowers/` scratchpad `coupling-map.md` (git-ignored); the modules named below are in `herdr_engineering/control_plane/`.

## Global Constraints

- `herdr_artifact_host` MUST NOT import `herdr_engineering` (enforced by a test, Task 1). Code is MOVED/copied in, never imported across.
- TDD; NO mocks/stubs/fakes; real Postgres and real HTTP. Run test files one at a time: `uv run --extra dev --extra collab --extra federation pytest <file> -q`.
- Public pages light only; tokens from `scripts/sync_share_tokens.py`. No dark mode.
- Dashboard `/library` is read-only and lists member artifacts only (not CI/agent reports).
- Share tokens and agent-token digests never leave the host; dashboard reads write nothing (no view events, claims, counters).
- Strong typing: enums not strings, no `any`-style dicts at boundaries (frozen dataclasses / TypedDict). Parameterized SQL only. Wire format MessagePack for service-to-service, JSON for browsers.
- Never `git add` .superpowers, egg-info, test-results, node_modules, uv.lock. Never commit to main. Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Do not delete the `artifacts.metabolomics.us` A record, retire `localdev.mcp`, or delete control-plane rows without the owner confirming.
- Members stay disabled in production until Task 12.

## Review Focus

1. Public role reaching an internal route (path confusion, `X-Forwarded-*`, `Host` tricks) — must 404.
2. Internal role accepting any non-GET, a wrong/missing key, or leaking share tokens / token digests.
3. Artifact HTML executing in the dashboard origin (content route must send `CSP: sandbox`, separate origin, iframe `sandbox`).
4. Import that silently drops or alters a blob/row (counts + sha256 must match, abort otherwise).
5. Host container able to resolve or reach any `herdr-cp-*` container or the control-plane Postgres.

---

### Task 1: Package scaffold, image, boundary tests

**Files:** Create `herdr_artifact_host/__init__.py`, `herdr_artifact_host/cli.py` (`main(argv)`, subcommands `public`, `internal`, `migrate`, `import`), `deploy/artifact-host/Dockerfile`, `tests/artifact_host/test_boundary.py`; modify `pyproject.toml` (package + `herdr-artifact-host` script, package-data `share_static`).

**Interfaces:** Produces `herdr_artifact_host.cli.main(argv: list[str]) -> int`; role enum `Role.PUBLIC | Role.INTERNAL`.

- [ ] Write failing `test_no_herdr_engineering_import`: AST-walk every `.py` under `herdr_artifact_host/`, assert no `import herdr_engineering`/`from herdr_engineering`.
- [ ] Write failing `test_internal_routes_absent_from_public_role` placeholder marked to be filled in Task 8 (skip is NOT allowed: assert the route tables are disjoint sets exposed by `routes_for(Role)`).
- [ ] Implement scaffold, `routes_for(role) -> frozenset[str]`; Dockerfile builds only this package (python:3.13-slim, non-root uid 10001, no control-plane code).
- [ ] Run tests; commit.

### Task 2: Move the self-contained share/render modules in

**Files:** Copy then adapt into `herdr_artifact_host/`: `share_session.py`, `share_lab.py`, `share_bundle.py`, `share_render.py`, `share_render_worker.py`, `share_static/`, `members_config.py`, `cognito.py` (from `collab_cognito.py`), `annotation_anchor.py`, `https_origin.py`; their existing tests moved to `tests/artifact_host/`. Drop the `share_rich_view`→`artifacts.ArtifactError` import (define `ContentError` locally).

**Interfaces:** Produces the same public names as the originals (claim cookie `CLAIM_DOMAIN`, `_CLAIM_KEY_LABEL` unchanged). Old files stay in the control plane until Task 13.

- [ ] Copy files; fix imports to package-relative; run the moved tests plus `test_share_tokens_match_dashboard`.
- [ ] Boundary test still green; commit.

### Task 3: Host database and blob store

**Files:** Create `herdr_artifact_host/db.py` (asyncpg pool, `Migrator`), `herdr_artifact_host/migrations/0001_init.sql`, `herdr_artifact_host/blobs.py` (`BlobStore(root: Path)`: `put(stream) -> Sha256`, `open(sha) -> BinaryIO`, `exists`), `tests/artifact_host/test_db.py`, `test_blobs.py`.

**Interfaces:** Tables (start from migrations 0041/0045–0051, member rows only): `artifacts`, `artifact_versions`, `artifact_blobs`, `artifact_acl`, `library_folders`, `library_items`, `share_claims`, `shares`, `share_events`, `annotations`, `member_usage`, `agent_tokens(digest, member_id, created_at, revoked_at)`, `schema_migrations`. `intake` enum column `accepted|pending|discarded`. BlobStore is content-addressed `sha256/xx/yy/<sha>`, writes via temp+rename, verifies hash.

- [ ] Failing tests: migrate twice is idempotent; blob put/get round-trips and a flipped byte on disk is detected on read; path traversal in sha refused.
- [ ] Implement; run against real Postgres (testcontainers); commit.

### Task 4: Library store (feed, folders, search, claims, intake)

**Files:** Create `herdr_artifact_host/library.py`, `tests/artifact_host/test_library.py`. Port logic from `artifact_library.py` (`_CANDIDATES_SQL`, `READABLE_SQL`, `feed`, `list_folders`, `file/unfile`, `record_claim`, accept/discard, `PENDING_CAP=25`) against the host schema.

**Interfaces:** `LibraryStore.feed(viewer: Viewer, query: FeedQuery) -> FeedPage`; `list_folders(owner)`, `count_pending(owner)`, `record_claim(...)` (refuses lab shares), `accept(item)`, `discard(item)`. `Viewer` is a frozen dataclass (member id or `OPERATOR_READ_ALL`). Same semantics as today: Assigned / Opened-via-link (public audience only) / Mine, filter chips, search, expired/revoked greyed.

- [ ] Port the existing library tests as failing tests first (same fixtures, new schema); add `OPERATOR_READ_ALL` sees every member's items.
- [ ] Implement; run; commit.

### Task 5: Member API, page and Cognito sessions (public role)

**Files:** Create `herdr_artifact_host/member_api.py`, `member_session.py`, `public_server.py` (routes: `/`, `/my/`, `/my/callback`, `/api/member/*`, `/s/*`, `/api/member/config`), port `member_web.py` and `share_public.py` logic with direct DB calls in place of view files and intents; uploads (chunked runs PUT, 17 MiB) write to BlobStore then DB in one transaction.

**Interfaces:** Consumes Tasks 2–4. Produces `PublicServer(config, db, blobs, library)`; sign-in verifies Cognito JWT (group `local.dev`), AEAD session and claim cookie as today.

- [ ] Port member_web/share_public tests to HTTP tests against a running `PublicServer` on a real port + real Postgres; add: lab share is not claimable; unknown vs revoked `/s/` bodies identical; claim cookie never valid as session and vice versa.
- [ ] Implement shares: create (pending), owner-only approve (user actor only), revoke, expiry default 30d max 90; events written directly (no jsonl hand-off).
- [ ] Run; commit.

### Task 6: Agent MCP at `/mcp/artifacts`

**Files:** Create `herdr_artifact_host/mcp.py`, `agent_tokens.py`, `tests/artifact_host/test_mcp.py`. Port the artifact tools from `collab_member_tools.py` (artifact_create, upload_chunk/status/abort, publish, delete, result, share_create, view tools, `artifact_search`; no share-approve tool).

**Interfaces:** Agent tokens minted from the member page (`POST /api/member/agent-tokens`), stored as digest, bound to one member; pushes land `intake='pending'`, 429 `pending_cap` at 25; protocol-version handling copied from `collab_mcp.py`.

- [ ] Failing tests with a real MCP client over HTTP: create+push lands pending; 26th pending is refused; revoked token 401; no approve tool listed.
- [ ] Implement; commit.

### Task 7: Background loop

**Files:** Create `herdr_artifact_host/housekeeping.py`, `tests/artifact_host/test_housekeeping.py`. Duties: expiry/revoke greying state, removed-member grace revocation (`groups_ttl` 24h, `removed_grace` 3600s), Cognito group list refresh from `members.json` dir, upload/staging cleanup, usage quotas.

**Interfaces:** `Housekeeper.run_once(now: datetime) -> SweepReport`, called every 60s by `public` role's loop.

- [ ] Failing tests with injected `now` (a parameter, not a mock): expired share greys item, removed member's shares revoke only after grace, disabled account at any age.
- [ ] Implement; commit.

### Task 8: Internal read API

**Files:** Create `herdr_artifact_host/internal_server.py`, `tests/artifact_host/test_internal.py`. Routes: `GET /v1/feed`, `/v1/folders`, `/v1/items/<id>`, `/v1/items/<id>/versions`, `/v1/shares`, `/v1/search`, `/v1/content/<item>/<version>/<path>`; MessagePack responses with `Accept: application/json` fallback.

**Interfaces:** Auth: bearer key, digest stored in config (`internal_key_digest`), role `dashboard-read` only; any non-GET → 405; no key/wrong key → 401 with identical body. `/v1/shares` returns id, item, audience, state, expiry — never token or URL. Content responses carry `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`. All reads run in `READ ONLY` transactions and write nothing.

- [ ] Failing tests: POST/PUT/DELETE → 405; wrong key → 401; shares response contains no token; row counts of `share_events`, `share_claims` unchanged after a full read sweep; every public-role route path returns 404 on the internal role and vice versa (uses `routes_for`).
- [ ] Implement; commit.

### Task 9: Dashboard integration (members only, read-only)

**Files:** Create `herdr_engineering/control_plane/artifact_host_client.py`; modify `artifact_library_api.py` (routes call the client, write routes removed), `packages/library-view.js` (remove write controls, show items from the host; deep links open `#/library/<id>`), `cli.py` (`_library_wiring` builds the client from `control_plane.artifact_library.host_url` / `host_key`; no `LibraryStore`), `fleet_config_cli.py` (new keys, validated), tests under `tests/control_plane/`.

**Interfaces:** `ArtifactHostClient.feed(query) / folders() / item(id) / versions(id) / shares() / content_url(...)`; failure → dashboard answers 503 for the library only. Content shown only in `<iframe sandbox>` (no `allow-same-origin`) pointing at the internal listener's own origin/port.

- [ ] Failing tests: dashboard `/library` rows equal the host's feed for the same fixture (real host process + real Postgres); host down → library 503, rest of dashboard unaffected; no write route exists; Playwright test that the library page renders and an artifact opens inside a sandboxed iframe.
- [ ] Implement; run `node --test tests/playwright/*.test.mjs`; commit.

### Task 10: Compose, networks, edge, deploy

**Files:** Create `deploy/artifact-host/compose.yaml` (services `artifact-host-db`, `artifact-host-public`, `artifact-host-internal`, volumes `artifact-host-blobs`, `artifact-host-db`; networks `artifact-host` (internal, no egress) and `artifact-internal` (joined also by `herdr-cp-web`)); modify `deploy/control-plane/deploy.sh` (build the new image from `deploy/artifact-host/Dockerfile`, bring up before the edge, prewarm `migrate`), `collab-edge/*.conf` (artifacts.local → `artifact-host-public:8090`; mcp.local `/mcp/artifacts` → host; nothing routes to internal), `fleet_config_cli.py` (render the host slice; keys: public_host, cognito, session_secret, claim key, internal_key_digest), `COLLAB.md` (new section), `tests/test_artifact_host_compose.py`.

**Interfaces:** Host containers publish no host ports; internal role binds only on `artifact-internal`.

- [ ] Failing tests: compose file parse asserts no host-port publish for host services, `artifact-host` network is `internal: true`, `herdr-cp-*` not attached to it; a real `docker compose up` smoke test asserts from inside `artifact-host-public` that `herdr-cp-web` and the control-plane Postgres do not resolve and `getent` fails.
- [ ] Implement; `deploy.sh --dry-run` renders; commit.

### Task 11: Import and verify tool

**Files:** Create `herdr_artifact_host/importer.py` (`import` subcommand), `tests/artifact_host/test_importer.py`.

**Interfaces:** `import --source-dsn <read-only DSN> --source-rustfs ...` reads rows where `created_by LIKE 'member:%'` plus `artifact_library_*`, `artifact_share*`, `artifact_annotations*`, `artifact_member_*`; copies blobs by hash; writes `manifest.json` (per-table counts, blob count, bytes, sha256 per blob); `verify` recomputes on the host side and exits non-zero on any difference. Idempotent (re-run safe), carries share-token digests so existing share URLs stay valid.

- [ ] Failing tests with a real source Postgres fixture: counts/hashes match; corrupted blob → verify fails closed; re-run is a no-op; zero-member source imports cleanly (today's production case).
- [ ] Implement; commit.

### Task 12: Deploy dark, cut over, enable members

**Files:** modify `fleet_config_cli.py` (drop the `members.enabled` ⇒ `api_keys` prerequisite for the host path), runbook `docs/superpowers/runbooks/artifact-host-cutover.md`.

- [ ] Review the whole branch with `/code-review` (opus 5.5 approved for security) before merge; merge via PR with CI green.
- [ ] Deploy with `deploy.sh --ref origin/main`; host dark; run `import` + `verify` against production (expected near-empty); back up first.
- [ ] Switch edge upstreams; enable members (fleet config: public_host, session_secret ≥32 bytes, internal key); no `api_keys` needed.
- [ ] Live checks: `/api/member/config` 200; `/s/unknown/` 404 identical body; landing bar + CSP; internal key from the dashboard works and a POST to the internal listener is 405; from the host container the control plane is unreachable; sign-in round trip; agent push lands pending; dashboard `/library` shows it read-only.
- [ ] Report to the owner; wait for the owner before DNS/old-host retirement.

### Task 13: Remove dead control-plane code (separate PR, after owner OK)

**Files:** delete member intents, worker ingestion, `collab-public` member routes, `LibraryStore` SQL, `share_public`/`share-public` container, old share volumes; update docs and tests.

- [ ] Only after cutover is verified and the owner confirms; full CI green; commit.
