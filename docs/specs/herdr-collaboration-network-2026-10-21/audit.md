# HCN P0 audit — evidence-based reuse map

Date: 2026-10-21. Repository: `berlinguyinca/herdr-engineering` (canonical per
`START_HERE.md` / ADR-0001), branch `feat/collab-network` off `origin/main`
(2a54942). Not pushed; no PR opened.

## Repository identification
| Repo | Finding |
|---|---|
| `herdr-engineering` (this) | Owned, editable. Python control plane (`herdr_engineering/control_plane/`), web client (`packages/`), Herdr native adapter. **Chosen home.** |
| `pi-engineering` (`~/IdeaProjects/pi-engineering`) | Owned TypeScript Pi extension/orchestrator (`src/aps`, `extensions/`). Mission authority per `START_HERE.md`. **Not modified**: the P1 slice is server-side; Pi adapter is P2. |
| Public `herdrdev/herdr` | External, read-only (ADR-0002). Not forked. |

## Reuse map
| Capability | Existing evidence | Class | Decision |
|---|---|---|---|
| HTTP/SSE server | `control_plane/api.py` `ControlPlaneServer`, `_Handler` | EXTEND | New `/api/v1/collab/*` branch dispatched before the legacy gate. |
| Auth | `api.py` `_role/_require`: global `X-API-Key` → viewer/operator/ingest; open mode = operator on loopback. No user identity, no tenant, no per-agent credential. | **NEW** (beside, not replacing) | Per-user bearer + per-agent credential. Legacy keys and open mode are **denied** on collab routes (they are shared, not identities). |
| Event log + durable sink | `repo.record_event` → `DurableEventSink` (`sink.py`) → `fabric_events` via `events.EventStore` | REUSE (sink/table), but **not** `record_event` | `record_event` feeds `/events/stream`, `/activity` and `/events?entity_type=` to every viewer key. Collab events use the same sink/table through `sink.record_required` with `entity_type="collab"`, kept out of `repo._events` (same technique as `inspector`, `repo.import_state`). |
| Idempotent command receipts | `repo.send_message` operation_id + sha256 fingerprint, 409-style conflict | REUSE pattern | Same key + different body = 409. |
| Agent→pane prompting | `send_message` → `_dispatch_native_message` → `NativeHerdrAdapter.send_input` → `herdr agent prompt` (types into a PTY) | **REJECT transport**, REUSE nothing | Peer messages are data in a room log, never delivered to a terminal. P2 bridge delivery must be a separate, scoped, local pull. |
| Ids | `control_plane/ids.py` (`_KINDS` frozenset) | EXTEND | Add `user, project, room, grant, invite, collab`. |
| Redaction | `control_plane/redact.py` | REUSE `redact_text` | Message text only. `redact_payload` is NOT applied to envelopes: its substring hints blank `auth*`, `token*`, `credential*`, `pass*` field names. |
| Rate limiting | `api._RateLimiter` | REUSE | Same per-IP limiter plus a per-principal message limiter. |
| CSRF/Origin/Content-Type | `api.do_POST` for `/api/v1/ci/`, `/reviews` | REUSE logic | Applied to all collab POSTs. |
| Hydration | `bridge.load_into` → `repo.import_state` | EXTEND | `import_state` diverts `collab` streams to the collab service (revocations survive restart). |
| Review/approval | `control_plane/reviews.py` (Plannotator receipts) | REUSE (P3) | Artifact review gates. |
| Artifact storage | `control_plane/artifacts.py` `ArtifactStore`, `rustfs.py`, `/api/v1/artifacts` | REUSE (P3) | No new object store. Note the current download endpoint has no per-project ACL: collab artifacts must not use it until P3 adds one. |
| Migrations | `deploy/control-plane/migrations/*.sql`, `db.Migrator` | NOT NEEDED for P1 | State is event-sourced in `fabric_events`; no new tables. Relational projection is a later option. |
| Web UI | `packages/*-view.js`, `herdr-web-client` | DEFER (P4) | |
| Observability | `observability.py`, `/api/v1/health` | EXTEND (partial) | `collab_v1` capability only when enabled; counters in `collab.stats()`. Prometheus export is next. |
| Herdr multi-machine | `herdr_adapter.py` (`list_machines`, `machine_targeted`) | REUSE (P2) | Possible bridge transport; audited as terminal-capable, so not exposed. |
| Memory/OpenViking | not in this repo | N/A | Never shared across users. |
| Federation / A2A | none | NEW (P2+) | Out of scope. |

## Baseline
`python -m pytest -q tests --ignore=tests/playwright --ignore=tests/control_plane/test_e2e_live_db.py` before any change: see STATUS.md entry.

## P1 blockers (user decisions; nothing in the repo answers them)
1. **SSO provider + MFA** for humans. Authenticator is an interface (`UserAuthenticator`); the shipped implementation is a static hashed-token registry for a two-user pilot only.
2. **Data classification** and whether cloud models may receive room content.
3. **Bridge topology / TLS.** Bearer tokens over plain HTTP are unsafe: the server refuses to enable collab on a non-loopback bind unless TLS termination is asserted.
4. Security-qualified threat-model review and guest pen-test (release gate in THREAT_MODEL.md).

Feature flag is **off by default**; none of the above blocks merging the flagged code, all block enabling it for real users.
