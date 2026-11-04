# Assistant Connectors tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the dashboard (Personal assistant → Connectors) the operator can Enable Gmail and Calendar with Google's own sign-in, have the assistant read mail/calendar, draft mail and create events after approval, block calendars, see Drafts, and set reminders delivered by phone push.

**Architecture:** A provider-based "apps" service (`assistant_apps.py`) owns per-person connections keyed `(tenant_id, user_id, provider)`; credentials are encrypted with a key file and stored in Postgres (memory fallback). Google is the first provider (OAuth web flow with one-time state + PKCE, callback on `https://local.metabolomics.us`); GitHub is a status-only provider. Assistant tools resolve the calling user's connection, so no tool can reach another person's tokens. A `CalendarPolicy` is the single choke point for blocked calendars. Reminders are a table plus a scheduler thread that sends push through the live `rakazo_push` notifier and posts a chat message.

**Tech Stack:** Python (stdlib `http.server` control plane, psycopg2, `cryptography` Fernet already installed), vanilla JS dashboard (`packages/personal-assistant-view.js`, `packages/herdr-web-client/client.js`), node:test + Playwright gates.

**Spec:** `docs/superpowers/specs/2026-10-30-assistant-connectors-tab-design.md`

## Global Constraints

- Person key is `(tenant_id, user_id)` from the existing assistant scope (`self._assistant_scope()` in api.py); the dashboard operator is `default/open-listener`.
- Scopes requested together at Enable: `gmail.readonly`, `gmail.compose`, `calendar.readonly`, `calendar.events`. Our code never calls Gmail send until a later feature.
- Config is YAML only, no new environment variables; secrets never in git, logs, API responses, tool results or chat.
- Redirect URI: `https://local.metabolomics.us/api/v1/assistant-apps/google/callback`.
- Writes (draft, event) are sensitive tools, never pre-authorized (add to `NEVER_PREAUTHORIZED`), approved through existing approval cards, and run only via `EXECUTABLE_AFTER_APPROVAL`.
- Mail/calendar text returned to the model carries `untrusted_content: true`.
- New dashboard UI must keep the accessibility ratchet and visual-regression gates green (record baselines in the same change).
- Merge flow in this repo: squash-merge PRs; the operator authorised merging without waiting for CI.

## Review Focus

- Blocked calendar given by alias (`primary`) or by real id, in list, read, free/busy and create: refused/omitted, test for each path.
- Callback with missing, reused, expired or another person's `state`: refused, nothing stored.
- Refresh token revoked or expired (Google "Testing" 7-day expiry): connection becomes `needs_attention`, tools return a fixed reason, no secret in the error.
- Key file missing/wrong: tab reports "not set up"; no save, no start failure.
- Reminder in the past, ambiguous time, or fired while the server was down: asks / fires late with a note, exactly once.
- Two people connected: person A's tool call can never use person B's tokens.

---

### Task 1: Credential store (encryption + tables)

**Files:**
- Create: `herdr_engineering/control_plane/app_credentials.py`, `deploy/control-plane/migrations/0043_assistant_apps.sql`
- Test: `tests/control_plane/test_app_credentials.py`

**Interfaces:**
- Produces: `class SecretBox(key_file: str|None)` with `.available: bool`, `.seal(plain: str) -> str`, `.open(token: str) -> str` (Fernet; `seal`/`open` raise `AppsError("not_set_up")` when unavailable).
- Produces: `class CredentialStore(box: SecretBox, persistence=None)` with `put_client(provider, client_id, client_secret)`, `get_client(provider) -> tuple[str,str]|None`, `put_connection(tenant, user, provider, *, account, scopes, refresh_token)`, `get_connection(tenant, user, provider) -> Connection|None`, `set_status(tenant, user, provider, status)` (`connected|needs_attention`), `delete_connection(...)`, `list_connections(tenant, user) -> list[Connection]` where `Connection` is a frozen dataclass `(provider, account, scopes, status, created_at)` that never holds a token; `refresh_token(tenant, user, provider) -> str|None`.
- Produces: `class AppsError(Exception)` with `.reason` fixed strings.
- Migration tables: `assistant_app_clients(provider PK, client_id, secret_ct)`, `assistant_app_connections(tenant_id, user_id, provider, account, scopes jsonb, status, token_ct, created_at, PK(tenant_id,user_id,provider))`, `assistant_app_state(state_sha256 PK, tenant_id, user_id, provider, verifier_ct, expires_at)`.

- [ ] **Step 1: Write failing tests** `test_seal_roundtrip_and_ciphertext_hides_plaintext`, `test_missing_or_short_key_file_is_not_available_and_save_is_refused`, `test_connection_never_exposes_token_in_repr_or_as_dict`, `test_two_people_do_not_see_each_others_connections`, `test_persistence_failure_does_not_lose_the_in_memory_connection` (assert on `.reason == "not_set_up"` and `"refresh" not in repr(conn)`).
- [ ] **Step 2: Run** `pytest tests/control_plane/test_app_credentials.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** the module and migration. Fernet key = urlsafe-b64 of the file's 32 bytes; Postgres persistence mirrors `PostgresPushPersistence` (short timeouts, DSN never logged).
- [ ] **Step 4: Run tests** → PASS; `ruff check` clean.
- [ ] **Step 5: Commit** `feat(apps): encrypted credential store for per-person app connections`.

### Task 2: Google OAuth web flow and provider interface

**Files:**
- Create: `herdr_engineering/control_plane/assistant_apps.py`, `herdr_engineering/control_plane/google_oauth.py`
- Modify: `herdr_engineering/control_plane/google_workspace.py` (keep `new_pkce`, `exchange_code` logic reused; remove file-token paths in Task 4)
- Test: `tests/control_plane/test_assistant_apps.py`

**Interfaces:**
- Consumes: Task 1 `CredentialStore`, `AppsError`.
- Produces: `class AppsService(store: CredentialStore, *, redirect_base: str, opener=None, clock=time.time)` with `providers() -> list[dict]` (id, name, kind `oauth|status`, connected, account, status, scopes), `save_client(provider, client_id, client_secret)`, `connect_start(tenant, user, provider) -> str` (authorization URL; stores one-time state, 10-minute expiry, with PKCE verifier sealed), `connect_callback(provider, params: dict) -> tuple[tenant,user]` (validates state exactly once, exchanges code, reads the account address from Gmail `users/me/profile`, stores connection; raises `AppsError("bad_state"|"expired"|"denied"|"unexpected_scope")`), `disconnect(tenant, user, provider)` (also revokes at Google, best effort).
- Produces: `GITHUB` status provider: `AppsService.providers()` includes `{"id":"github","kind":"status",...}` fed by a callable `github_status_fn() -> dict` passed at construction.

- [ ] **Step 1: Write failing tests** `test_connect_start_url_has_four_scopes_pkce_and_state_and_no_secret`, `test_callback_with_unknown_reused_expired_or_foreign_state_is_refused_and_stores_nothing`, `test_callback_stores_connection_for_the_person_who_started_it`, `test_callback_refuses_a_token_with_extra_scopes`, `test_disconnect_deletes_and_second_disconnect_is_quiet`, `test_providers_listing_never_contains_secrets`, `test_github_status_provider_is_listed_read_only`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with an injected `opener` (as `google_workspace.GoogleWorkspace` does) so tests use a fake Google.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(apps): provider interface and Google OAuth web flow`.

### Task 3: HTTP routes and server wiring

**Files:**
- Modify: `herdr_engineering/control_plane/api.py` (GET/POST/PUT/DELETE under `/api/v1/assistant-apps`; unauthenticated GET `/api/v1/assistant-apps/google/callback`), `herdr_engineering/control_plane/assistant.py` (`apps` config block via `apps_config`), `herdr_engineering/cli.py` (pass the Postgres DSN like `rakazo_bridge_dsn`)
- Test: `tests/control_plane/test_assistant_apps_api.py`

**Interfaces:**
- Consumes: Task 2 `AppsService`.
- Produces routes (JSON): `GET /api/v1/assistant-apps` → `{ready: bool, redirect_uri, providers: [...], blocked_calendars: [...]}`; `PUT /api/v1/assistant-apps/google/client {client_id, client_secret}`; `POST /api/v1/assistant-apps/google/connect` → `{url}`; `GET /api/v1/assistant-apps/google/callback?state&code` → 302 to `/#/personal-assistant` (with `?connected=google` or `?error=<reason>`); `DELETE /api/v1/assistant-apps/google`; `GET|PUT /api/v1/assistant-apps/google/blocked-calendars`; `GET /api/v1/assistant-apps/google/calendars` (names + ids, for the picker).
- Config: `personal_assistant.apps: {key_file: <path>, redirect_base: https://local.metabolomics.us}`; `apps_config(raw)` validates, unknown keys refused.

- [ ] **Step 1: Write failing tests** `test_list_reports_not_ready_without_a_key_file`, `test_put_client_stores_secret_and_responses_never_echo_it`, `test_connect_returns_a_google_url_for_the_calling_person`, `test_callback_redirects_and_a_bad_state_redirects_with_error_not_500`, `test_callback_needs_no_assistant_scope_but_connect_does`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** following the `assistant-connectors` routing pattern (api.py ~lines 4091/4239). **Step 4: Run** → PASS. **Step 5: Commit** `feat(apps): /api/v1/assistant-apps routes`.

### Task 4: Per-person Google access and CalendarPolicy

**Files:**
- Modify: `herdr_engineering/control_plane/google_workspace.py` (replace file token with `GoogleAccess(creds: CredentialStore, tenant, user)`; refresh via stored client; `invalid_grant` → `set_status(needs_attention)` and `GoogleError("needs_attention")`)
- Create: `herdr_engineering/control_plane/calendar_policy.py`
- Test: `tests/control_plane/test_calendar_policy.py`, update `tests/control_plane/test_google_workspace.py`

**Interfaces:**
- Produces: `class CalendarPolicy(blocked: set[str], resolve_primary: Callable[[], str])` with `.allows(calendar_id) -> bool` (resolves `primary` to the real id first), `.filter(calendars: list[dict]) -> list[dict]`, `.require(calendar_id)` raising `GoogleError("calendar_blocked")`.
- Produces: `GoogleWorkspace.run(tenant, user, tool_id, payload)` unchanged signature, now resolving credentials from `CredentialStore`; blocked list from `AppsService.blocked_calendars(tenant, user)`.

- [ ] **Step 1: Write failing tests** `test_primary_alias_and_real_id_are_blocked_together`, `test_list_free_busy_read_and_create_all_refuse_a_blocked_calendar`, `test_model_supplied_blocked_id_gets_the_generic_error`, `test_revoked_refresh_token_marks_needs_attention_without_leaking`, `test_person_a_tool_call_cannot_use_person_b_tokens`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS (including the previous google tests, adapted). **Step 5: Commit** `feat(apps): per-person Google access and blocked-calendar policy`.

### Task 5: Read tools and free/busy

**Files:**
- Modify: `herdr_engineering/control_plane/assistant.py` (TOOL_CATALOG: add `find_free_time`; `search_email`, `read_email`, `list_calendar_events` already exist and now go through Task 4), `herdr_engineering/control_plane/google_workspace.py` (`find_free_time(duration_minutes, time_min, time_max)` using Calendar `freeBusy` over allowed calendars only)
- Test: `tests/control_plane/test_google_workspace.py`

**Interfaces:**
- Produces: tool `find_free_time {duration_minutes:int, time_min:str, time_max:str}` → `{slots:[{start,end}]}` (max 5).

- [ ] **Step 1:** failing tests `test_free_time_ignores_blocked_calendars`, `test_free_time_returns_at_most_five_slots_inside_the_window`. **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** PASS. **Step 5:** commit `feat(apps): find_free_time over allowed calendars`.

### Task 6: Write tools (draft, event) and the Drafts API

**Files:**
- Modify: `herdr_engineering/control_plane/assistant.py` (tools `create_email_draft {to, subject, body}` and `create_calendar_event {summary, start, end, calendar_id?, location?, reminder_minutes?}`, both `sensitive: True`, both in `NEVER_PREAUTHORIZED` and `EXECUTABLE_AFTER_APPROVAL`; executor calls `google_write`), `herdr_engineering/control_plane/google_workspace.py` (`create_draft`, `list_drafts`, `delete_draft`, `create_event`; **no send function exists**), `api.py` (`GET /api/v1/assistant-apps/google/drafts`, `DELETE .../drafts/<id>`)
- Test: `tests/control_plane/test_google_writes.py`

**Interfaces:**
- Produces: `GoogleWorkspace.create_draft(tenant,user,to,subject,body) -> {id}`; `list_drafts(...) -> [{id,to,subject,snippet,gmail_url}]`; `create_event(...) -> {id,html_link}` refusing blocked calendars and non-primary default.

- [ ] **Step 1:** failing tests `test_draft_and_event_tools_are_sensitive_never_preauthorized_and_run_only_after_approval`, `test_unapproved_call_creates_nothing`, `test_event_with_reminder_minutes_sets_a_popup_override`, `test_event_on_a_blocked_calendar_is_refused_before_any_request`, `test_module_exposes_no_send_function` (assert no attribute containing "send"), `test_drafts_list_and_discard_use_the_calling_persons_connection`.
- [ ] **Step 2–5:** run FAIL, implement, PASS, commit `feat(apps): approval-gated drafts and events; drafts API`.

### Task 7: Reminders

**Files:**
- Create: `herdr_engineering/control_plane/reminders.py`, `deploy/control-plane/migrations/0044_assistant_reminders.sql`
- Modify: `assistant.py` (tools `set_reminder {text, when}`, `list_reminders`, `cancel_reminder {id}`, all non-sensitive), `api.py` (`GET /api/v1/assistant-reminders`, `DELETE /api/v1/assistant-reminders/<id>`; start the scheduler with the server), `rakazo_bridge.py` (small `notify_person(tenant,user,title,body)` using `PushNotifier`)
- Test: `tests/control_plane/test_reminders.py`

**Interfaces:**
- Produces: `class ReminderStore` (`add(tenant,user,text,fire_at) -> Reminder`, `due(now) -> list`, `mark_fired(id)`, `cancel(id, tenant, user)`, `list(tenant,user)`), `class ReminderScheduler(store, deliver: Callable[[Reminder, bool], None], clock, interval=15)` with `start()/stop()/tick()`; `deliver(reminder, late: bool)` posts the chat message and push.
- `when` parsing: ISO datetime or relative (`in 20 minutes`, `tomorrow 09:00`) in the person's timezone from assistant notification settings; unparsable → tool error asking for a clearer time.

- [ ] **Step 1:** failing tests `test_due_reminder_fires_exactly_once`, `test_reminder_missed_during_downtime_fires_late_with_the_late_flag`, `test_past_time_is_refused_with_a_question_not_stored`, `test_cancel_only_by_the_owner`, `test_reminders_are_delivered_even_in_quiet_hours`, `test_no_google_connection_needed`.
- [ ] **Step 2–5:** run FAIL, implement, PASS, commit `feat(assistant): reminders delivered by push and chat`.

### Task 8: Dashboard — Apps, blocked calendars, Drafts, Reminders

**Files:**
- Modify: `packages/personal-assistant-view.js` (the existing `Connectors` tab: add an **Apps** section above the existing connector list, a one-time settings form for the Google client id/secret with the redirect URI shown and copyable, per-card Enable/Disconnect/"Sign in again", a blocked-calendars checklist, a **Drafts** list (Open in Gmail, Discard, disabled Send "coming soon"), a **Reminders** list (Cancel); handle `?connected=` / `?error=` on return), `packages/herdr-web-client/client.js` (methods `listApps`, `saveAppClient`, `connectApp`, `disconnectApp`, `listCalendars`, `setBlockedCalendars`, `listDrafts`, `discardDraft`, `listReminders`, `cancelReminder`), `packages/personal-assistant.css`
- Test: `packages/herdr-web-client/assistant-client.test.js`, `tests/playwright/assistant-connectors.test.mjs`, a11y ratchet counts and visual baselines per repo convention

**Interfaces:**
- Consumes: routes from Tasks 3, 6, 7.

- [ ] **Step 1:** failing client tests (each method hits the exact URL with the right verb) and a Playwright test with a fixture server: not set up → shows "not set up"; connected card shows the account; needs-attention shows "Sign in again"; Enable navigates to the returned Google URL; blocked checklist saves; Drafts Send is disabled; no response body in the page contains the fixture secret string.
- [ ] **Step 2–4:** run FAIL, implement (escape all text with the file's `esc`), run PASS, record visual baselines and accessibility counts the way the repo's gates require (`docs/` and existing tests show the procedure).
- [ ] **Step 5:** commit `feat(dashboard): Apps, Drafts and Reminders in the assistant Connectors tab`.

### Task 9: Deploy plumbing, retire the file-based path, docs, rollout

**Files:**
- Modify: `deploy/control-plane/deploy.sh`, `deploy/control-plane/compose.yaml` (replace the `google` mount with an `apps` mount: `~/.config/herdr-engineering/apps/` holding only `apps.key`, mode 0600), `tests/test_deploy_scripts.py`, `tests/test_compose_render.py`, `herdr_engineering/cli.py` (remove `herdr-eng google authorize`), `herdr_engineering/control_plane/assistant.py` (remove `google_config`), `deploy/control-plane/README.md` (document the Google client setup and the redirect URI)
- Test: updated deploy tests

- [ ] **Step 1:** update/add failing deploy tests (apps dir created 0700, only `apps.key`, bad mode refused, mounted read-only into web only).
- [ ] **Step 2–4:** run, implement, PASS; run the full `tests/test_deploy_scripts.py tests/test_compose_render.py` and the Rakazo/assistant suites.
- [ ] **Step 5:** commit, open PR, merge (operator-authorised), then deploy from a worktree at the same ref, create the key file (32 random bytes, 0600, never printed), set `personal_assistant.apps` in the fleet YAML (backup first), and tell the operator to create the Google client, register the redirect URI, paste it in the tab, and press Enable. Save a memory note.

## Self-review

- Spec coverage: Connectors tab (Task 8), Enable per app (2,3,8), Gmail read/draft + Drafts view (5,6,8), send later (no send function, disabled Send button, Task 6/8), Calendar read/create-after-approval (5,6), blocked calendars (4,8), per-person (1,2,4), GitHub card (2,8), reminders (7,8), encryption/key handling and "not set up" state (1,3), Google Testing-mode expiry as `needs_attention` (4,8), retire #503 (9), gates (8).
- Types: `Connection`, `AppsService`, `CalendarPolicy`, `ReminderStore` names are used consistently across tasks.
- Proportion: tasks specify interfaces and tests; bodies are left to the implementer.
