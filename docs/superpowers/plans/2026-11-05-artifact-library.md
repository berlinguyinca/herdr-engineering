# Artifact Library Implementation Plan (phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every member one library of their artifacts (mine, assigned, opened via link, pending agent pushes) with search, filters and personal folders, on the public host and in the Herdr dashboard, restyled to the dashboard design, with agents able to push into it.

**Architecture:** A new `artifact_library` module (Postgres) owns folders, claims and the unified feed query. The dashboard API reads it directly through SQL. The public member web has no database, so writes go through the existing worker intent system and the feed is published into the per-member view file that `MemberService` already reads. Public search and filtering run in the browser over that bounded view; the dashboard uses SQL.

**Tech Stack:** Python 3 (asyncpg, PostgreSQL migrations), vanilla ES modules (`packages/`), node:test, Playwright through `node --test tests/playwright/*.test.mjs`.

**Spec:** `docs/superpowers/specs/2026-11-05-public-artifacts-frontend-design.md`

## Global Constraints

- Light theme only. Tokens: background #f7f7f8, surface #ffffff, border #d9d9e3, text #353740, text-secondary #6e6e80, accent #10a37f, radius 8px, Inter with system fallback. No dark mode on the public side.
- Strict CSP stays: no CDN, no external requests. Fonts and CSS are same-origin.
- The dashboard stays Tailscale-only (`bender.tail0c50da.ts.net:9447`) and is never routed publicly.
- Folders are personal and per member, many-to-many, and never change visibility, copy content, or re-share.
- Agent-pushed artifacts are created Pending, visible only to their owner; max 25 Pending per member, further pushes refused with a clear message; no expiry; only the owner accepts or discards; Pending counts toward member limits. Human web creation skips Pending.
- Expired or revoked rows show title, sharer and date only, no content, no open action. Claim snapshots title and sharer; expiry and revocation are read live.
- One row per artifact; badge priority Mine, then Assigned, then Opened via link.
- The token never goes into library storage. The sign-in return path is held in sessionStorage, not a query string.
- Repo rules: TDD (failing test first), no mocks/stubs/fakes (real Postgres via `tests/control_plane/registry_pg.py`, real HTTP servers), four test layers (unit, integration, smoke, E2E), run Python test files one at a time (`python -m pytest <file> -q`), new migrations additive with a `-- DOWN:` header, next number `0051`.
- Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never `git add` anything under `.superpowers`, `test-results`, `node_modules` or `*.egg-info`. Never commit to main.

## Review Focus

- A member with no artifacts, shares or claims: the feed is an empty list with a friendly empty state, not an error.
- A share revoked or expired after the member claimed it: the row stays, greyed, with no open action and no content (Task 3, Task 8).
- Someone else's artifact id or folder id passed to any library route: refused with the same response as a missing one, never leaking existence (Tasks 2, 3, 6).
- An agent that pushes the 26th Pending artifact, or pushes twice with the same idempotency key (Task 5).
- A search string with SQL wildcards, quotes or a very long value, and a feed past its page size (Task 4).

---

## File Structure

- Create `deploy/control-plane/migrations/0051_artifact_library.sql`: folders, folder items, claims, Pending flag.
- Create `herdr_engineering/control_plane/artifact_library.py`: `LibraryStore` (SQL), types, caps.
- Create `herdr_engineering/control_plane/artifact_library_api.py`: dashboard HTTP routes under `/api/v1/library`.
- Modify `herdr_engineering/control_plane/artifact_member_intents.py`: intents for folders, claims, pending; publish the feed into `write_member_view`.
- Modify `herdr_engineering/control_plane/artifact_member_service.py` and `member_web.py`: library routes, content-by-id route.
- Modify `herdr_engineering/control_plane/share_public.py`: landing bar, fonts, CSP, claim hook.
- Modify `herdr_engineering/control_plane/collab_member_tools.py`: Pending on agent create, `artifact_search`, accept/discard not exposed to agents.
- Create `packages/library-view.js` (+ test): dashboard page. Modify `packages/app.js`, `packages/herdr-web-client/client.js`.
- Modify `packages/member-artifacts-app.js`, `member-artifacts-view.js`, `member-artifacts.css`, `share_static/*`: public restyle and library UI.
- Create `share_static/tokens.css`, `share_static/fonts/inter-*.woff2`.
- Tests under `tests/control_plane/`, `packages/*.test.js`, `tests/playwright/`.

---

### Task 1: Migration 0051 and schema contract

**Files:**
- Create: `deploy/control-plane/migrations/0051_artifact_library.sql`
- Test: `tests/control_plane/test_artifact_library_migration.py`

**Interfaces:**
- Produces tables: `artifact_library_folders(folder_id text pk 'fld_<hex24>', owner text, name text, created_at, UNIQUE(owner, lower(name)))`; `artifact_library_items(folder_id, artifact_id, source text CHECK in ('manual','suggested','auto'), filed_at, PK(folder_id, artifact_id))`, FK to folders ON DELETE CASCADE; `artifact_share_claims(member_id text, share_id text, title text, sharer text, claimed_at, PK(member_id, share_id))`; column `artifacts.intake text NOT NULL DEFAULT 'accepted' CHECK (intake IN ('accepted','pending'))`.

- [ ] **Step 1: Write failing tests** in `test_artifact_library_migration.py` using the `registry_pg` fixture: `test_tables_exist_after_all_migrations`, `test_source_check_rejects_unknown_value` (insert source `'bogus'` raises CheckViolation), `test_deleting_folder_cascades_items_not_artifacts`, `test_intake_defaults_to_accepted_for_existing_rows`, `test_folder_name_unique_per_owner_case_insensitive`.
- [ ] **Step 2: Run** `python -m pytest tests/control_plane/test_artifact_library_migration.py -q`. Expected: FAIL (tables missing).
- [ ] **Step 3: Write the migration** (additive, `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, `-- DOWN:` header, indexes on `(owner)`, `(artifact_id)`, `(member_id, claimed_at DESC)`, partial index on `artifacts(created_by) WHERE intake='pending'`).
- [ ] **Step 4: Run the test file.** Expected: PASS. Also run `python -m pytest tests/control_plane/test_artifact_binding_migration.py -q` to confirm earlier migrations still apply.
- [ ] **Step 5: Commit** `feat(library): migration 0051 for folders, claims and pending intake`.

### Task 2: LibraryStore folders and filing

**Files:**
- Create: `herdr_engineering/control_plane/artifact_library.py`
- Test: `tests/control_plane/test_artifact_library_store.py`

**Interfaces:**
- Produces: `class LibraryStore(pool)`; `@dataclass(frozen=True) Folder(folder_id: str, owner: str, name: str, item_count: int)`; `FolderSource = Literal["manual","suggested","auto"]`; errors `LibraryError(status: int, code: str)`; methods `async create_folder(owner: str, name: str) -> Folder`, `rename_folder(owner, folder_id, name) -> Folder`, `delete_folder(owner, folder_id) -> None`, `list_folders(owner) -> list[Folder]`, `file_artifact(owner, folder_id, artifact_id, *, source: FolderSource = "manual") -> None`, `unfile_artifact(owner, folder_id, artifact_id) -> None`.
- Consumes: Task 1 tables; visibility rule from `artifacts.list_records` (owned or ACL read/admin, tombstoned excluded) for "may file this artifact".

- [ ] **Step 1: Write failing tests**: `test_create_rename_delete_folder`, `test_duplicate_name_refused_409`, `test_one_artifact_in_two_folders`, `test_cannot_file_an_artifact_the_member_cannot_read` (404, same as missing), `test_other_members_folder_id_is_404`, `test_delete_folder_keeps_artifacts`, `test_name_length_and_blank_refused` (blank and >80 chars → 400).
- [ ] **Step 2: Run** the file. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the store with parameterised asyncpg queries; names trimmed, 1–80 chars; ids `fld_` + 24 hex.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): folder store`.

### Task 3: Claims and the unified feed

**Files:**
- Modify: `herdr_engineering/control_plane/artifact_library.py`
- Test: `tests/control_plane/test_artifact_library_feed.py`

**Interfaces:**
- Produces: `Source = Literal["mine","assigned","opened","pending"]`; `ItemStatus = Literal["active","expired","revoked","pending"]`; `@dataclass(frozen=True) FeedItem(artifact_id: str, title: str, sharer: str, shared_at: datetime, source: Source, status: ItemStatus, expires_at: datetime | None, share_id: str | None, folder_ids: tuple[str, ...], artifact_type: str, size_bytes: int)`; `@dataclass(frozen=True) FeedPage(items: list[FeedItem], total: int, next_offset: int | None)`; `async record_claim(member_id: str, share_id: str) -> None` (idempotent; snapshots title and sharer; refuses a non-live share with 404); `async feed(member_id: str, member_email: str, member_groups: frozenset[str], *, q=None, sources: frozenset[Source] | None = None, folder_id=None, artifact_type=None, since=None, until=None, limit: int = 50, offset: int = 0) -> FeedPage`.
- Assigned = direct `artifact_acl` grants plus live lab-audience shares whose `allow_emails` or `allow_groups` match the member. Opened = rows in `artifact_share_claims`. Mine = `created_by = member:<id>` with `intake='accepted'`. Pending = `intake='pending'`, owner only.

- [ ] **Step 1: Write failing tests**: `test_empty_member_gets_empty_feed`, `test_owned_direct_lab_and_claimed_all_appear`, `test_lab_share_for_other_group_not_listed`, `test_dedupe_one_row_badge_priority_mine_over_assigned_over_opened`, `test_revoked_share_stays_as_revoked_row_with_snapshot_title`, `test_expired_share_status_expired_read_live`, `test_pending_only_visible_to_owner`, `test_pending_excluded_unless_source_includes_pending`, `test_claim_of_pending_or_revoked_share_404`, `test_claim_is_idempotent_and_keeps_first_snapshot`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** `record_claim` and `feed` as one visibility-first SQL (UNION of the four sources, ranked per artifact by badge priority, ordered `shared_at DESC`), filtering before `LIMIT`.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): claims and unified feed`.

### Task 4: Search and filters

**Files:**
- Modify: `herdr_engineering/control_plane/artifact_library.py`
- Test: `tests/control_plane/test_artifact_library_search.py`

**Interfaces:**
- Consumes: `feed(...)` from Task 3. Produces: the `q`, `folder_id`, `artifact_type`, `since`, `until` arguments working. `q` matches title, original filename, sharer, folder names and artifact type, case-insensitive, escaped (`%`, `_`, `\` are literal), maximum 200 chars (longer → `LibraryError(400, "query_too_long")`).

- [ ] **Step 1: Write failing tests**: `test_q_matches_title_filename_sharer_and_folder_name`, `test_percent_and_underscore_are_literal`, `test_quote_and_semicolon_do_not_break_query`, `test_q_longer_than_200_refused`, `test_combined_filters_are_anded` (source + folder + type + date), `test_paging_stable_and_total_correct_across_pages` (120 rows, limit 50), `test_search_never_returns_artifacts_member_cannot_see`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** with parameterised `ILIKE ... ESCAPE '\'`; no string interpolation of user text.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): search and filters`.

### Task 5: Pending intake for agent pushes

**Files:**
- Modify: `herdr_engineering/control_plane/artifact_library.py`, `artifact_member_intents.py`, `collab_member_tools.py`
- Test: `tests/control_plane/test_artifact_library_pending.py`

**Interfaces:**
- Produces: `PENDING_CAP = 25`; `async count_pending(owner: str) -> int`; `async accept_pending(owner: str, artifact_id: str) -> None`; `async discard_pending(owner: str, artifact_id: str) -> None` (deletes via the existing member delete path); `PendingCapExceeded(LibraryError)` with status 429, code `pending_cap`, message naming the cap. The agent `artifact_create` intent writes `intake='pending'` when the caller is an agent and `accepted` for humans. Accept and discard are not offered as agent tools.

- [ ] **Step 1: Write failing tests**: `test_agent_create_is_pending_human_create_is_accepted`, `test_26th_pending_refused_with_clear_message`, `test_same_idempotency_key_does_not_double_count`, `test_accept_moves_to_mine`, `test_discard_deletes_and_frees_a_slot`, `test_other_member_cannot_accept_or_discard_404`, `test_pending_counts_toward_member_byte_limit`, `test_agent_tool_menu_has_no_accept_or_discard`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** in the intents module following `register_intent_kind` at lines 270–277 for `library_accept` and `library_discard`; enforce the cap inside the create intent in the same transaction as the insert.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): pending intake for agent pushes`.

### Task 6: Worker intents and the member view feed

**Files:**
- Modify: `herdr_engineering/control_plane/artifact_member_intents.py` (`write_member_view`, line ~584), `artifact_member_service.py`
- Test: `tests/control_plane/test_member_library_intents.py`

**Interfaces:**
- Produces intent kinds: `library_folder_create`, `library_folder_rename`, `library_folder_delete`, `library_file`, `library_unfile`, `library_claim`, `library_accept`, `library_discard`, each registered like the existing kinds. `write_member_view` adds key `library` = `{items: [FeedItem as dict], folders: [Folder as dict]}` capped at `LIBRARY_VIEW_MAX = 2000` items, newest first, and `MemberService.library(actor) -> dict` returns it.

- [ ] **Step 1: Write failing tests** against the real worker and a real Postgres: `test_each_intent_updates_db_and_view_file`, `test_view_file_contains_pending_only_for_owner`, `test_view_capped_at_2000_newest_first`, `test_replayed_intent_is_idempotent`, `test_intent_for_other_members_folder_fails_404`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** the intents and extend `write_member_view`.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): worker intents and library view`.

### Task 7: Member web routes and content-by-id

**Files:**
- Modify: `herdr_engineering/control_plane/member_web.py` (`_route` line 332, `_BODY_KEYS`), `artifact_member_service.py` (`can_read` line 169)
- Test: `tests/control_plane/test_member_library_web.py`

**Interfaces:**
- Produces routes under the existing `/api/member/` prefix: `GET library`, `POST folders`, `PUT folders/<id>` (rename), `DELETE folders/<id>`, `POST folders/<id>/items` (body `{artifact_id}`), `DELETE folders/<id>/items/<artifact_id>`, `POST claims` (body `{path: "/s/<token>/"}` — the server hashes the token and looks the share up by `token_hash`; the token is never stored), `POST pending/<artifact_id>/accept`, `POST pending/<artifact_id>/discard`, and `GET artifacts/<id>/content/<path>` serving bytes of the head version when `can_read` (owner, ACL, live lab share matching the member, or a claim whose share is still live); responses use the existing `_send` helpers, sandboxing CSP and `X-Content-Type-Options: nosniff`.

- [ ] **Step 1: Write failing tests** (real `ControlPlaneServer` + `MemberWeb` over HTTP like `test_member_web.py`): `test_library_requires_session_401`, `test_folder_routes_round_trip`, `test_claim_with_live_token_path_records_row_without_storing_token`, `test_claim_with_garbage_path_404`, `test_content_by_id_for_owner_assigned_and_claimed`, `test_content_by_id_refused_after_revoke_and_expiry`, `test_content_by_id_for_stranger_404_same_as_missing`, `test_content_has_sandbox_csp_and_nosniff`, `test_origin_check_applies_to_all_writes`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** the routes as `_r_*` methods; writes enqueue the Task 6 intents and return the ticket pattern already used by create/publish.
- [ ] **Step 4: Run** the file, then `python -m pytest tests/control_plane/test_member_web.py -q`. Expected: PASS both.
- [ ] **Step 5: Commit** `feat(library): member web routes and content by id`.

### Task 8: Public landing page, claim flow and sign-in

**Files:**
- Modify: `herdr_engineering/control_plane/share_public.py` (`STATIC_FILES` line 152, `VIEWER_CSP`, `VIEW_CSP` line 186, `do_GET` line 1187), `share_static/viewer.html`, `view.html`, `share.js`, `lab-signin.js`
- Test: `tests/control_plane/test_share_public_landing.py`, `packages/share-landing.test.js`

**Interfaces:**
- Produces: a top bar in `viewer.html` and `view.html` reading "Shared by NAME · title · expires in N days"; signed-out visitors get the notice "Sign in with your lab account to add this to your library. Signing in does not change who can see it." and a button "Sign in to keep this"; signed-in visitors trigger `POST /api/member/claims` once per page load. The return path is stored in `sessionStorage` key `herdr.share.return` before the Cognito redirect and read after `/s/callback`; never in a query string. The sign-in card reads "Sign in to Artifacts", "Lab members: use your lab account. Opening a shared link doesn't need an account.", button "Continue with lab sign-in".

- [ ] **Step 1: Write failing tests**: Python `test_viewer_has_shared_by_bar_and_signin_notice_when_signed_out`, `test_notice_absent_when_signed_in`, `test_viewer_csp_allows_same_origin_font_but_nothing_external`, `test_return_path_not_in_any_url`; JS `renderLandingBar` unit tests (`expires in 29 days`, `expires today`, missing sharer shows "Shared by a lab member"), `claim posts once and tolerates 404`.
- [ ] **Step 2: Run** each file separately. Expected: FAIL.
- [ ] **Step 3: Implement** bar, notice, claim call and CSP `font-src {origin}/s/static/`; add new static files to `STATIC_FILES` and `pyproject.toml` package-data.
- [ ] **Step 4: Run** both files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): public landing bar, claim and sign-in copy`.

### Task 9: Design tokens, fonts and the public restyle

**Files:**
- Create: `herdr_engineering/control_plane/share_static/tokens.css`, `share_static/fonts/inter-latin-400.woff2`, `inter-latin-600.woff2`
- Modify: `share_static/share.css`, `packages/member-artifacts.css`, `member_web.py` `_STATIC` and CSP (add `font-src 'self'`, style path for tokens)
- Test: `tests/control_plane/test_share_tokens_match_dashboard.py`

**Interfaces:**
- Produces: `tokens.css` containing the light token values only, generated from `packages/herdr-design-system/theme.css` by `scripts/sync_share_tokens.py` (exit 1 with `--check` when out of date). Dark-mode `prefers-color-scheme` rules removed from `share.css`.

- [ ] **Step 1: Write failing tests**: `test_tokens_css_equals_dashboard_light_tokens`, `test_no_prefers_color_scheme_dark_rules_in_public_css`, `test_fonts_served_same_origin_with_font_mime_and_cache_header`, `test_member_page_csp_has_font_src_self_and_no_external_host`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** the sync script, add the font files (Inter, OFL licence file alongside), restyle `share.css` and `member-artifacts.css` with the tokens (8px radius, surface cards, accent buttons, pill chips, badge classes `badge-mine|assigned|opened|pending|expired`).
- [ ] **Step 4: Run** the file, then `node --test packages/member-artifacts-view.test.js`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): dashboard tokens and Inter on the public side`.

### Task 10: Public library UI (feed, chips, search, folders, Pending)

**Files:**
- Modify: `packages/member-artifacts-app.js` (route table ~line 240), `member-artifacts-view.js`, `member-api.js`, `member-artifacts.html`
- Test: `packages/member-artifacts-view.test.js`, new `packages/member-library.test.js`

**Interfaces:**
- Produces pure functions in `member-artifacts-view.js`: `filterFeed(items, {q, sources, folderId, type, since, until}) -> items` (case-insensitive, same rules as Task 4), `renderFeedRow(item) -> string` (escapes all text; expired/revoked rows have class `row-muted`, an "Expired" or "Revoked" tag and no open link), `renderChips(counts, active) -> string` (Pending chip only when `counts.pending > 0`), `renderFolderSidebar(folders, activeId) -> string`; API methods `library()`, `createFolder`, `renameFolder`, `deleteFolder`, `fileItem`, `unfileItem`, `acceptPending`, `discardPending` in `member-api.js`.

- [ ] **Step 1: Write failing tests**: `filterFeed` source/folder/type/date/text combinations; `renderFeedRow` escapes `<script>` in titles and sharers; expired row has no `href`; badge text per source; empty state text "Nothing here yet" for each filter; Pending chip hidden when zero; Accept and Discard buttons only on Pending rows.
- [ ] **Step 2: Run** each test file with `node --test <file>`. Expected: FAIL.
- [ ] **Step 3: Implement** the view functions and wire the app: feed at `#/`, folder views at `#/f/<id>`, search input debounce 150 ms, chips and folder sidebar; open actions go through `GET artifacts/<id>/content/` from Task 7.
- [ ] **Step 4: Run** both test files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): public library UI`.

### Task 11: Dashboard API and page

**Files:**
- Create: `herdr_engineering/control_plane/artifact_library_api.py`, `packages/library-view.js`, `packages/library-view.test.js`
- Modify: `herdr_engineering/control_plane/api.py` (mount), `packages/app.js` (route near line 230, nav near line 99), `packages/herdr-web-client/client.js` (+ test)
- Test: `tests/control_plane/test_artifact_library_api.py`

**Interfaces:**
- Produces routes `GET /api/v1/library`, `POST/PUT/DELETE /api/v1/library/folders...`, `POST /api/v1/library/items`, `POST /api/v1/library/pending/<id>/accept|discard` calling `LibraryStore` directly for the dashboard's authenticated viewer; client methods `listLibrary(params)`, `createFolder`, `renameFolder`, `deleteFolder`, `fileItem`, `unfileItem`, `acceptPending`, `discardPending`; dashboard route `'/library': viewLibrary` under the Artifacts nav group, sharing `filterFeed` and `renderFeedRow` so both surfaces render identically.

- [ ] **Step 1: Write failing tests**: Python `test_dashboard_library_requires_auth`, `test_viewer_only_sees_own_feed`, `test_folder_crud_through_api`, `test_pending_accept_through_api`; JS `listLibrary builds the query string and escapes q`, view renders the same row markup as the public view for one fixture.
- [ ] **Step 2: Run** each file separately. Expected: FAIL.
- [ ] **Step 3: Implement** the API module, client methods and `packages/library-view.js` (`mountLibrary`), using the dashboard's own sign-in and design system.
- [ ] **Step 4: Run** all three files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): dashboard library page and API`.

### Task 12: Agent search tool

**Files:**
- Modify: `herdr_engineering/control_plane/collab_member_tools.py`
- Test: `tests/control_plane/test_collab_member_search_tool.py`

**Interfaces:**
- Produces MCP tool `artifact_search` with args `{q?: string, source?: "mine"|"assigned"|"opened", folder?: string, type?: string, limit?: int 1–50}` returning `{items: [{artifact_id, title, source, status, folders}], total}`. Pending items are never returned (agents already know what they pushed). Backed by `LibraryStore.feed` for the agent's owning member.

- [ ] **Step 1: Write failing tests**: `test_tool_listed_for_member_agents_only`, `test_search_returns_owner_visible_items_only`, `test_pending_not_returned`, `test_args_validated_unknown_key_refused`, `test_limit_above_50_refused`.
- [ ] **Step 2: Run** the file. Expected: FAIL.
- [ ] **Step 3: Implement** the schema entry in `MEMBER_TOOLS` and the dispatch in `call`.
- [ ] **Step 4: Run** the file and `python -m pytest tests/control_plane/test_collab_member_tools.py -q` if present. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): artifact_search member tool`.

### Task 13: End-to-end, smoke and security gate

**Files:**
- Create: `tests/playwright/library.test.mjs`, `tests/playwright/library-server.py`, `tests/control_plane/test_library_security.py`
- Modify: `package.json` (add to `test:ui` if not globbed)

**Interfaces:**
- Consumes everything above. The E2E server script runs the real `MemberWeb` with a real Postgres-backed store (not the in-memory service) and a real worker.

- [ ] **Step 1: Write failing tests**: Playwright `library.test.mjs`: sign in, see Mine/Assigned/Opened rows, filter by chip, search, create a folder and file one artifact into two folders, accept a Pending push, open a public link signed out and see the notice, sign in and see the claim appear, revoke the share and see the greyed row; Python security: `test_stranger_cannot_read_folder_claim_pending_or_content_by_id`, `test_no_external_requests_in_page_csp`, `test_xss_titles_escaped_in_both_surfaces`, `test_token_not_in_database_or_view_file_after_claim`, `test_error_bodies_identical_for_missing_and_forbidden`.
- [ ] **Step 2: Run** `node --test tests/playwright/library.test.mjs` and `python -m pytest tests/control_plane/test_library_security.py -q`. Expected: FAIL.
- [ ] **Step 3: Fix** whatever the tests expose in earlier tasks' code; no new features.
- [ ] **Step 4: Run** both, then `npm run test:client`, `npm run test:ui`, and the control_plane suite one file at a time. Expected: PASS.
- [ ] **Step 5: Dispatch the security review** (opus 5.5, approved by the user) on the whole branch diff; fix every confirmed finding; re-run the tests; commit `test(library): end-to-end and security tests`.

### Task 14: Deploy, integrate and verify

**Files:**
- Modify: `~/.config/herdr-engineering/fleet/fleet-config.yaml` (members enabled with prerequisites), docs `docs/COLLAB.md`, `packages/collab-guide.md`; no secrets in git.
- Create: `docs/operations/artifact-library.md`

**Interfaces:**
- Consumes the merged branch. Order is fixed: (1) land PR #645 so `mcp.local.metabolomics.us` is served by the edge; (2) open the library PR, CI green, review with opus 5.5, merge; (3) back up the fleet config; set `control_plane.collab.members.enabled: true` with its prerequisites (`share.enabled`, `collab_artifacts.enabled`, `api_keys`) and `share.public_host` as the design requires; (4) `deploy/control-plane/deploy.sh --dry-run --ref <ref>` then the real deploy; (5) repoint agents' MCP configs to `https://mcp.local.metabolomics.us/`.

- [ ] **Step 1: Write the failing check**: add `scripts/verify_library_deploy.sh` that curls the public and agent hosts and exits non-zero on any miss (`artifacts.local` `/` → 200 sign-in page; `/s/unknown/` → 404; `mcp.local` `/api/v1/collab/config` → 200; `localdev.mcp` → 200 during overlap).
- [ ] **Step 2: Run it before deploy.** Expected: FAIL on the new library endpoints.
- [ ] **Step 3: Merge #645, merge the library PR, apply the config, dry-run then deploy** (stop and report on any failure; the deploy script rolls back).
- [ ] **Step 4: Run the verify script and a live check**: sign in at the public host, push an artifact from an agent over `mcp.local`, see it Pending, accept it, find it by search, file it in two folders, view the same library in the dashboard on Tailscale. Expected: all succeed.
- [ ] **Step 5: Commit** docs and the verify script; delete the `artifacts.metabolomics.us` A record only after the user confirms the timing.

---

## Self-review notes

- Spec coverage: feed and badges (3), claim and sign-in reminder (8), restyle light only (9), search and filters (4, 10), folders (2, 10), Pending (5), agent push and search (5, 12), dashboard (11), hosting and agents (14), assistant filing deferred to phase 2 (the `source` column in Task 1 keeps room for it).
- Rulings carried: public search runs client-side over the capped view (Tasks 6, 10); dashboard search is SQL (Task 4); no repo-wide coverage gate is added.
- Names to keep identical across tasks: `LibraryStore`, `FeedItem`, `Source`, `ItemStatus`, `PENDING_CAP`, `LIBRARY_VIEW_MAX`, `filterFeed`, `renderFeedRow`.
