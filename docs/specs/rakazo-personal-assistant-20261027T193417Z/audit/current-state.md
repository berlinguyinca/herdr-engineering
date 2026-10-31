# Current-state audit — Herdr Personal Assistant (Rakazo integration)

Phase P0 (mandatory audit-first) of the spec bundle
(`docs/specs/rakazo-personal-assistant-20261027T193417Z/`).

Audit performed **2026-10-27** against the live working tree on branch
`feat/rakazo-personal-assistant` (base `origin/main` @ `9464e90`). This audit
was performed against the **actual repository on this host**, not the spec's
2026-10-27 snapshot. Where the spec assumed an interface that does not match the
tree, the discrepancy is recorded under "integration hazard".

Upstream Rakazo reference: pinned read-only checkout at
`/home/wohlgemuth/upstream-refs/rakazo` @ `c03cd4a9` (Apache-2.0, 2026-10-27,
"Release mobile 1.0.10 (#2474)"). No Rakazo code was copied into this change.

---

## 1. Feature matrix (desired → existing owner)

Columns: implemented? · owner module/file · API or event · maturity · existing
tests · action (`reuse` | `refactor` | `adapt` | `new`) · integration hazard.

| Desired feature | Implemented? | Owner module/file | API / event | Maturity | Tests | Action | Hazard |
|---|---|---|---|---|---|---|---|
| Control-plane HTTP + SSE server | Yes | `herdr_engineering/control_plane/api.py` (`ControlPlaneServer`, `_Handler._route_api`) | `/api/v1/*`, SSE | production | `tests/control_plane/test_api.py` | **reuse** | must not add second server |
| In-memory repo persistence | Yes | `herdr_engineering/control_plane/repo.py` (`ControlPlaneRepo`) | n/a | production | `test_api.py` | **reuse** | no duplicate DB |
| Postgres migrations | Yes | `herdr_engineering/control_plane/db.py` (`Migrator`) | SQL migrations | production | `tests/control_plane/test_t16_*_postgres.py` | **reuse** | assistant tables go through this path |
| Role-based auth (viewer/operator/ingest/agent/reviewer) | Yes | `api.py` `_Handler._role/_require`, `_make_handler` | header `X-API-Key` | production | `test_api.py` | **reuse** | no separate login stack (D1) |
| User/tenant scope headers | New (added) | `api.py` `_Handler._assistant_scope` | `X-Herdr-User`, `X-Herdr-Tenant` | new | `test_assistant_api.py` | **new** | must never trust body for identity |
| Scheduler / placement | Yes | `herdr_engineering/scheduler.py`, `control_plane/scheduler_service.py` | `/api/v1/scheduler` | production | `test_scheduler_service.py` | **reuse** | no second scheduler |
| Host placement + blacklist policy | Yes | `herdr_engineering/host_policy.py`, `fleet.py`, `fabric.py` | config | production | `tests/control_plane/test_host_scheduler_policy.py` | **reuse** | laptop/manual placement must be honored |
| Artifact registry + ACL | Yes | `herdr_engineering/control_plane/artifacts.py`, `artifact_acl.py` | `/api/v1/artifacts`, `/api/v1/artifact-acl` | production | `test_artifacts_api.py`, `test_artifact_acl.py` | **reuse** | assistant artifacts register here, not a new store |
| Notification channel | Yes | `herdr_engineering/attention.py`, `control_plane/attention_inbox.py` | `/api/v1/attention`, SSE | production | `test_attention_*.py` | **reuse** | no ad hoc push |
| Audit event bus | Yes | `repo.record_event`, `observability.py`, `journal.py` | repo events | production | — | **reuse** | assistant audit appends here, no parallel logging |
| Secrets store | Yes | `herdr_engineering/config.py`, `session_env.py`, control-plane secrets | config | production | — | **reuse** | tokens never in UI/logs |
| UI stack (vanilla JS / Web Components + tokens) | Yes | `packages/app.js`, `packages/herdr-web-components/`, `packages/herdr-design-system/` | `/api/v1` client | production | `packages/*.test.js` (`node --test`) | **reuse** | do not import Rakazo React stack |
| Settings nav + route pattern | Yes | `packages/app.js` (`NAV`, `ROUTES`, `visibleNav`) | hash route | production | — | **reuse** | assistant adds a route, not a new app |
| Model routing (InferWeave) | Partial | `herdr_engineering/llm_proxy.py`, `pi_autospec.py` | `/api/v1/llm` | production | `test_llm*` | **adapt** | assistant model selection rides this; no second router |
| Assistant entity / thread / message / run / grant / approval / memory | **No** | — | — | planned | — | **new** (this change) | implemented in `assistant.py` |
| Tool gateway (deny-by-default + approvals) | **No** | — | — | planned | — | **new** | `assistant.py` `request_tool_action` |
| Persistent assistant chat surface | **No** | — | — | planned | — | **new** | `personal-assistant-view.js` |
| Rakazo agent runner | No (upstream has one) | upstream `packages/core` | — | upstream beta | upstream | **adapt** (skeleton adapter) | do not vendor; adapt behind versioned runner |
| Rakazo memory | No (upstream has one) | upstream `packages/memory` | — | upstream beta | upstream | **adapt** | prefer Herdr canonical store |
| Rakazo chat-ui / web / auth / db / worker | No | upstream `apps/web`, `packages/auth`, `packages/db` | — | upstream beta | upstream | **exclude** | never import the second stack |

## 2. Upstream Rakazo feature matrix (c03cd4a9)

| Upstream capability | Reuse decision | Note |
|---|---|---|
| `packages/core` agent lifecycle + Pi bridge | **Adapt** behind `RuntimeAdapter` (ADR-0003) | versioned; InferWeave route is a later phase |
| `packages/memory` contracts | **Adapt** concepts only | canonical storage stays in Herdr `assistant.py` |
| `packages/adapter-kit`, `packages/adapters` (MCP/OpenAPI/computers) | **Evaluate in P4** | after threat assessment; scopes/secret isolation required |
| `apps/web`, `packages/chat-ui`, `packages/ui-web`, `packages/ui-tokens` | **Exclude** | do not import React stack into the vanilla JS dashboard |
| `apps/mobile` | **Evaluate in P5** | responsive-first now; native later |
| `packages/auth` (Better Auth) | **Exclude** | reuse Herdr control-plane auth (D1) |
| `packages/db`, worker scheduler | **Exclude** | no second DB / scheduler |

License: Apache-2.0. No code is copied in this change, so no NOTICE obligation
is triggered yet; **if any upstream file is copied in a later phase, retain the
Apache NOTICE and record the SHA** (see `THIRD_PARTY_NOTICES.md` in the repo
root for the existing pattern).

## 3. Reconciliation decisions (locked)

- **D1 identity**: reuse the control plane's authenticated principal. Assistant
  data is additionally scoped by `(tenant_id, user_id)` supplied via
  `X-Herdr-User` / `X-Herdr-Tenant` headers (never from the request body). The
  dashboard operator defaults to `user=operator`, `tenant=default`.
- **D2 persistence**: assistant entities live in a canonical in-memory store on
  `ControlPlaneRepo` (`assistant.py` `AssistantStore`), the same persistence
  layer the control plane uses. No new DB server. Postgres-backed migration is a
  later-phase concern through `db.py`.
- **D3 runtime**: P1/P2 ship a versioned `RuntimeAdapter` skeleton with a
  deterministic `LocalRunnerAdapter`. The InferWeave model route is a P3 wiring
  concern; the skeleton is explicitly marked as non-production inference.
- **D4 UI**: reuse the existing vanilla JS / Web Components stack, `hd-*`
  primitives and theme tokens. Assistant adds one capability-gated nav route and
  one settings surface; no new component library.

## 4. Audit evidence (file:line / commit)

- Control-plane router dispatch: `herdr_engineering/control_plane/api.py`
  `_route_api` (line ~1443), `_api_get` (~1744), `_api_post` (~2106),
  `_assistant_get`/`_assistant_post` (added in this change).
- POST body is read once in `_api_post` (line ~2170); assistant POST routes must
  reuse that `body` rather than re-reading (keep-alive safety).
- Only `do_GET`/`do_POST` existed (lines 1160/1183); `do_PATCH`/`do_DELETE`
  were added in this change so the spec's PATCH/DELETE routes work.
- Health capabilities list: `api.py` `_health` (~line 617); `assistant_v1`
  appended when the assistant feature is enabled.
- Frontend nav + routes: `packages/app.js` `NAV` (~line 68), `ROUTES` (~line 140).
- Feature-flag config pattern: `herdr_engineering/control_plane/assistant.py`
  `assistant_config` (enabled defaults False). Inertness enforced by
  `_require_enabled` on every entry point.
