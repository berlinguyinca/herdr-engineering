# Implementation ledger — Herdr Personal Assistant (Rakazo integration)

Living record of decisions, modified files, test output, failures, remaining
risks and rollback notes. Updated each phase.

## 2026-10-27 — P3 durable routines, approvals and memory

### What was added
- `AssistantRoutine` entity + store support (`routines`, `routine_fired` dedupe set).
- Routine domain methods: `create/list/get/update/archive_routine`, `due_routines`,
  `trigger` (idempotent per schedule bucket), `_compute_next` (timezone via
  `zoneinfo`, DST-aware; quiet-hours [start,end) with wrap-past-midnight;
  `interval` and `daily` trigger types). No new daemon: `due_routines` is polled
  by the control plane (or a later phase wires the existing scheduler).
- `correct_memory` (bump revision + provenance; future retrieval uses new revision).
- API: `/api/v1/assistant-routines` GET/POST/PATCH/DELETE + trigger; memory PATCH.
- Frontend: Routines tab (create interval/daily routine, timezone, quiet hours,
  run/archive); memory correction via PATCH.
- Tests: +2 unit (routines), +2 contract, +4 client (routines + memory correction).

### Test output
```
python -m pytest tests/control_plane/test_assistant_service.py tests/control_plane/test_assistant_api.py -q
# => 49 passed (was 38)
node --test packages/herdr-web-client/*.test.js
# => 255 passed (was 251)
ruff check ... assistant.py api.py tests/...
# => All checks passed
```
Regression: core control-plane tests still pass (97 backend incl. assistant).
UI re-rendered at 1280/375 with the Routines tab; DOM dump confirms it.

## 2026-10-28 — P5 voice/mobile + P6 hardening

### P5 — voice and mobile
- `packages/voice.js`: pure capability helpers + `createVoiceInput` (Web Speech
  Recognition) and `speak` (SpeechSynthesis), both degrading to text-only.
- Chat composer: mic (voice input) + speaker (voice output) buttons; hidden when
  the browser lacks the API.
- Mobile CSS: 480px media query (stacked composer, full-width controls,
  comfortable touch targets). Verified at 375px and 1280px.

### P6 — hardening
- `redact_secrets()` scrubs auth headers / api keys / tokens / passwords / secrets;
  applied to the audit log (action + detail) and, defense-in-depth, to connector
  errors in `connector_request`.
- Input/config validation: connector `base_url` + allowlist origins must be https;
  routine timezone must resolve via `zoneinfo`; interval amount range-checked.
- Per-key fixed-window rate limiter (`max_requests_per_minute`, default 120) wired
  into the assistant GET/POST handlers; HTTP 429 on exceed (`RateLimited`).
- Cross-user isolation tests for connectors/routines/delegations.
- Tests: +7 unit (redaction, validation, isolation, rate limit), +1 contract (429),
  +7 client (voice). 127 backend, 266 client green; ruff clean.

## 2026-10-28 — P4 connectors, computer tools, delegation

### What was added
- `Connector` entity + registry; `HttpConnectorAdapter` (deny-by-default):
  https-only, operator-declared SSRF allowlist (`_allowed_origins`), credentials
  stored but never serialized/logged (only `auth_present`), response truncation.
- Computer-tool sandbox policy in `assistant_config["computer"]`:
  `allow_shell`/`allow_browser` default **false**; `allowed_hosts`/`allowed_commands`
  allowlists; `max_timeout_seconds`. `browse`/`run_shell` evaluate the policy and
  deny by default; the skeleton performs no real execution (honest).
- `delegate` tool + `DelegationRequest` (queued→running→succeeded/failed/cancelled)
  with artifact-ref registration; never fabricates completion. `execute_tool`
  dispatches granted tools.
- API: `/api/v1/assistant-connectors` (register/list/get/archive/request),
  `/api/v1/assistant-delegations` (create/list/get/cancel/artifact).
- Frontend: Connectors + Delegations tabs.
- Tests: +9 unit (connectors SSRF/allowlist/credentials, policy, delegation),
  +2 contract, +4 client. 109 backend, 259 client green; ruff clean.

## 2026-10-27 — P0 audit + P1 domain/authorization + P2 settings/chat MVP

### Decisions taken (see `audit/adr-*.md`)
- ADR-0001 assistant domain/persistence on the existing `ControlPlaneRepo`.
- ADR-0002 authorization rides control-plane auth; `X-Herdr-User`/`Tenant` scope.
- ADR-0003 versioned `RuntimeAdapter`; deterministic `LocalRunnerAdapter` for now.

### Files changed (this slice)
- `herdr_engineering/control_plane/assistant.py` — **new**: domain + policy service
  (assistants, threads, messages, runs, actions, grants, approvals, memory),
  tool catalog, feature flag, runtime adapter, audit log.
- `herdr_engineering/control_plane/api.py` — wired `server.assistant`; added
  `/api/v1/assistants`, `/assistant-runs`, `/assistant-approvals`,
  `/assistant-memory` GET/POST/PATCH/DELETE routes; added `do_PATCH`/`do_DELETE`;
  `assistant_v1` health capability when enabled.
- `packages/herdr-web-client/client.js` — identity headers + assistant client methods.
- `packages/personal-assistant-view.js` — **new**: settings + chat view.
- `packages/personal-assistant.css` — **new**: feature-scoped styles (theme tokens).
- `packages/app.js` — nav entry (`/personal-assistant`, capability-gated), route, view.
- `packages/index.html` — stylesheet include.
- `tests/control_plane/test_assistant_service.py` — **new**: 27 unit tests.
- `tests/control_plane/test_assistant_api.py` — **new**: 11 contract tests.
- `packages/herdr-web-client/assistant-client.test.js` — **new**: 5 client tests.
- `docs/specs/rakazo-personal-assistant-20261027T193417Z/audit/current-state.md`,
  `adr-0001..0003`, `ledger.md`, `operator-guide.md`, `PHASES.md`, `screenshots/*`.

### Test commands + output
```
# backend unit + contract
python -m pytest tests/control_plane/test_assistant_service.py tests/control_plane/test_assistant_api.py -q
# => 38 passed

# regression (existing core control-plane tests that collect on this host)
python -m pytest tests/control_plane/test_api.py tests/control_plane/test_handoff.py \
  tests/control_plane/test_scheduler_persistence.py tests/control_plane/test_scheduler_service.py \
  tests/control_plane/test_agent_experience_api.py -q
# => 58 passed

# frontend client tests
node --test packages/herdr-web-client/*.test.js
# => 246 passed (incl. 5 new assistant client tests)
```
> Note: many `tests/control_plane/test_collab_*` and `test_t16_*_postgres.py`
> modules do not collect on this host because the `jwt` package and collab
> fixtures are not installed in the venv (`ModuleNotFoundError: No module named
> 'jwt'`). This is a pre-existing environment gap, not a regression: the failing
> modules are unrelated to the assistant feature and fail at import time.

### Lint
```
ruff check herdr_engineering/control_plane/assistant.py herdr_engineering/control_plane/api.py \
  tests/control_plane/test_assistant_service.py tests/control_plane/test_assistant_api.py
# => All checks passed!
```

### UI verification
- Render harness: `packages/personal-assistant-demo.html` (serves the real view
  against a mock backend). Rendered with headless Chrome at **1280×900** and
  **375×812**; screenshots saved to `screenshots/` (desktop + mobile). DOM dump
  confirms the view mounted (header, tabs, thread, chat, tool list).
- Screenshots are of the real view + design tokens, not a mockup.

### Failures encountered and resolved
1. **Mission MSN-JWzelc implementer worker failed at model init** ("Failed to
   initialize samplers: failed to parse grammar"; "operator switched models").
   Not a code issue. Mission canceled by operator; implementation done directly.
2. `_read_json` double-read: `_api_post` reads the POST body once; `_assistant_post`
   initially read it again → empty body / 400. Fixed by passing the already-read
   `body` into `_assistant_post`.
3. PATCH/DELETE returned 501 (no `do_PATCH`/`do_DELETE`). Added them, routing
   through the operator-gated router.
4. Frontend `sendMessage` name collision: assistant method overwrote the session
   `sendMessage`. Renamed to `sendAssistantMessage`.
5. `_q` helper takes 2 args; corrected the `?q=` / `?state=` parsing.

## Remaining risks (tracked)
- **R1** Durability: assistant store is in-memory (ADR-0001). Postgres-backed
  migration deferred.
- **R2** Real inference: `LocalRunnerAdapter` is a skeleton; InferWeave route,
  async queueing and checkpoint/resume are P3+.
- **R3** User identity: `X-Herdr-User` is a trusted internal header; per-user
  OIDC mapping is deferred.
- **R4** P3–P6 not implemented in this slice: durable routines on the scheduler,
  real memory backend, connectors/computer tools/Pi-mission delegation, voice,
  mobile native, production hardening. See `PHASES.md`.

## Rollback
- Feature flag `personal_assistant.enabled` defaults **false**; flipping it off
  stops all assistant service entry points (`_require_enabled`) and hides the
  nav route (capability not advertised). No background runs are scheduled.
- No destructive DB actions; no production deploy performed. To fully remove the
  slice, revert this branch; the store is in-memory so no migration rollback is
  required.
