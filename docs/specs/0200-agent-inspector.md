# 0200 — Agent inspector (split-view integration)

**Status:** implemented · **Contract:** [`agent-inspector-event`](../contracts/agent-inspector-event.md) · **ADR:** [0006](../adr/ADR-0006-capability-gated-reasoning-visibility.md)

Extends Herdr's **existing session detail view** (`#/herd/session/:id`) with a
unified agent inspector. Nothing is replaced: the session summary, attach
command, steering box and event stream remain; the inspector mounts beneath
them. Pi-side telemetry and the optional terminal split view live in
`berlinguyinca/pi-engineering` (`docs/split-view-and-inspector.md`).

## Existing vs. new (reuse matrix)

| Capability | Before | Now |
|---|---|---|
| Per-agent identity, host, mission, branch, cwd, attach | ✅ `session` entity, plugin feed | reused unchanged |
| Transport / auth / roles / rate limit | ✅ `POST /api/v1/ingest`, `viewer/operator/ingest` | reused; `entity_type=inspector` added |
| Redaction | ✅ `control_plane/redact.py` | reused (per-field; see below) |
| Durable event log | ✅ `fabric_events` sink | reused for summary kinds only |
| Conversation / tools / files / tests / usage views | ❌ (only coarse `SessionToolCalled`) | **new** (`inspector.py`, `inspector.js`) |
| Reasoning view | ❌ (ADR-0005 forbade) | **new, capability-gated** (ADR-0006) |
| Streaming tool status | ❌ | **new** (per-session SSE, in-place entries) |
| Searchable history | ❌ | **new** (server-side, role-aware) |
| File changes + diff inspection | ❌ | **new** |
| Agent/subagent attribution | ❌ | **new** (`agent_id`, `parent_agent_id`) |
| Test run + failure reporting | partial (`/api/v1/tests` adapter view) | **new** per-agent runs with failures |
| Mission / handoff / worktree / PR links | mission+branch only | **new** `association` kind |
| Context & token usage | ✅ fleet analytics | per-agent usage + context % added |
| Per-agent/global UI prefs, retention | ❌ | **new** (persisted via durable sink) |
| Responsive layout | ✅ app shell | inspector `auto/split/stacked` |
| Bounded buffers | ❌ (raw streams unbounded) | **new** for inspector data |

Also fixed: `viewSession` read the wrong path segment (`parts[1]`), so the
session detail page 404'd; and the HTTP listen backlog (5) reset connections
under bursts of ≥ ~10 agents — now 256.

## Behaviour

- **Streaming:** `GET /api/v1/sessions/{id}/inspector/stream` (SSE, per session).
  The global activity SSE is untouched, so inspector traffic cannot flood it.
  Slow clients lose frames, never memory: queues are bounded (256) and overflow
  produces a `gap` frame that triggers a `since_rev` resync.
- **Bounds (defaults → hard ceiling):** 2 000 events (50 000), 4 MB (64 MB) per
  session; 64 KB per body, 256 KB per diff; 256 sessions (1 024); 256 MB total
  (1 GB); 24 h retention (720 h). Oldest entries are evicted first; ended
  sessions are evicted before live ones. Counters (`dropped`, `evicted`,
  `client_dropped`) are always surfaced — gaps are shown, never hidden.
- **Access:** reasoning and `sensitive` bodies are withheld from roles below
  `operator` (metadata stays visible). Sensitive = emitter flag, secret-looking
  paths (`.env`, keys, `.ssh`, …) or commands (`printenv`, `cat .env`, …).
  Withheld content is excluded from search for that role (no oracle).
- **Redaction:** applied to every text field at ingest. The ingest route skips
  the blanket key-hint redaction for `inspector` envelopes because it would
  mangle counters such as `input_tokens` and `passed`.
- **Time:** retention runs on the server's receive clock; client `ts` is display-only
  and clamped to `[now − 720 h, now + 5 min]`, so a bogus timestamp cannot pin or shorten retention.
- **Resync:** responses and the stream's `connected` frame carry a process `epoch`;
  the UI does a full reload on reconnect, gap, epoch change, or when an incremental
  fetch would skip changes (`has_more`). Client buffer: ≤ 500 rows (≤ 1 000 after
  "Show more"); older history is reached via search.
- **Missing/interrupted telemetry:** no telemetry → explicit empty state;
  stale (>120 s without events) → banner; restart/interrupt → running tools are
  marked `interrupted`, never successful.

## Configuration

Server environment (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `HERDR_INSPECTOR_REASONING_ROLE` | `operator` | minimum role to read reasoning (`viewer`\|`operator`) |
| `HERDR_INSPECTOR_SENSITIVE_ROLE` | `operator` | minimum role to read sensitive output/diffs |
| `HERDR_INSPECTOR_STORE_REASONING` | `1` | `0` = never accept reasoning |
| `HERDR_INSPECTOR_MAX_EVENTS` / `_MAX_BYTES` / `_MAX_SESSIONS` / `_MAX_TOTAL_BYTES` / `_RETENTION_HOURS` | see above | global limits (clamped to ceilings) |

Preferences (UI → Preferences, or `POST /api/v1/inspector/preferences` and
`/sessions/{id}/inspector/preferences`, operator role): `layout`, `density`,
`default_view`, `show_reasoning` (default off), `show_tool_output`,
`tool_output_lines`, `follow_tail`, and per-session retention
(`max_events_per_session`, `max_bytes_per_session`, `retention_hours`).

## Authentication: with and without a token

The inspector adds **no new credential**. Pick the control-plane mode you already run:

| Mode | How | Pi hosts need | Who can read reasoning / sensitive bodies |
|---|---|---|---|
| **Tailnet-trust (no token)** — the shipped `deploy/control-plane` default | server runs `--allow-open`, reachable only on the tailnet / loopback | only `HERDR_CP_URL` + `PI_INSPECTOR_FEED=1` | **everyone who can reach the port** (open mode = operator for all) |
| Keyed | `--api-keys 'ik=ingest,vk=viewer,ok=operator'` | the above + `HERDR_CP_API_KEY=ik` (ingest role: write-only for `/ingest`) | operators only (default) |

Tokenless is a *network* trust decision, not an application one: anyone on the tailnet can
post events for any session id and read everything, including reasoning if an agent opted in.
If that is not acceptable, either use keyed mode or keep reasoning off the control plane
(`PI_INSPECTOR_REASONING=off`, the default, and/or `HERDR_INSPECTOR_STORE_REASONING=0` on the
server). Verified end to end: the real Pi feed with no key reaches an open control plane, and a
keyed server answers 401 to the same request (`tests/control_plane/test_inspector.py`).

## Install

1. Deploy this branch's control plane (no schema migration; no new dependency).
2. Enable the Pi feed per host (see pi-engineering docs): set `HERDR_CP_URL`
   and `PI_INSPECTOR_FEED=1` (optionally the reasoning opt-in).
3. Open any session in the dashboard; the inspector appears under the summary.

## Rollback

- UI only: revert `packages/inspector-view.js`, the 3 lines in `app.js` and the
  `.hd-ins*` CSS — the session page returns to its previous form.
- Telemetry: unset `PI_INSPECTOR_FEED` on hosts (feed stops; nothing else
  changes). The control plane simply holds no inspector data.
- Server: the `inspector` entity type is additive; older control planes ignore
  it via the generic "unknown entity" path. Durable `inspector.*` rows are inert
  to older versions (ignored on hydrate) and can be pruned with
  `DELETE FROM fabric_events WHERE entity_type='inspector'`.

## Tests

`tests/control_plane/test_inspector.py` (store, bounds, ACL, restore, HTTP, SSE,
64-agent load), `packages/herdr-web-components/inspector.test.js` (renderers,
XSS, paging), `tests/playwright/ui.test.mjs` (real browser: tabs, streaming,
search, resize, gating). Run serially: `pytest`, `node --test --test-concurrency=1 …`.
