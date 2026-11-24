# Backend handoff: slices 01 + 03 + 04

Branch `feat/agent-operations-map`. Commits:
- `9859827f` agent-ops: optional provenance/session/model envelope fields + event schema (slice 01)
- `f7c1bf73` agent-ops: snapshot/events/replay/stream projection behind agent_ops_v1 (slices 03+04)

## Enabling
Config `control_plane.agent_ops.enabled: true` (strict boolean true; YAML only). When off, every
`/api/v1/agent-ops/*` route returns 404 and `agent_ops_v1` is absent from `GET /api/v1/health` `capabilities`.

## Auth
API key (`X-API-Key`, viewer role or higher) is required as for other routes. Room visibility comes from the
collab identity: send `Authorization: Bearer <collab token>`. An invalid collab token gives 401. An API key
with the operator role and a configured `server.operator_user` reads as that user. An API key alone sees
only org-scoped, room-less events (no rooms, no room events). Authorization runs before the fold, so
counts, rooms and edges never include unauthorized data.

## Endpoints (GET)
- `/api/v1/agent-ops/snapshot` : current projection.
- `/api/v1/agent-ops/events?cursor=<c>&limit=<1..500, default 100>` : `{events, next_cursor, has_more}`; events are
  metadata-only normalized events ordered by `(ingested_at, event_id)`; cursor is exclusive.
- `/api/v1/agent-ops/replay?until=<cursor>` : snapshot folded only up to the cursor (also accepts `cursor=`).
- `/api/v1/agent-ops/stream` : SSE. First frame `event: connected` with `data: {"cursor": ...}`. Then
  each event: `id: <cursor>` / `data: <event json>`. Resume with `Last-Event-ID` header or `?cursor=`.
  Polls once per second, `: keep-alive` comments when idle, closes after the max SSE duration.
  Permissions are re-evaluated each tick.

## Dismissing an agent (POST)
`POST /api/v1/agent-ops/dismiss`, operator API key only (viewer and ingest keys get 403; with `agent_ops` off it is the
same 404 as the GET routes; the capability stays `agent_ops_v1`). JSON body: `agent_id` and/or `session_id` (each
`[A-Za-z0-9._:@-]{1,160}`, at least one) and an optional `reason` (string, at most 200 characters; stored, never
projected). 200 `{"dismissed": <session_id or agent_id>, "cursor": <cursor of the tombstone>}`; 404 when no stored event
names the target (or it is a `user:` endpoint); 400 on a bad body.

It records a durable envelope-v2 event `session.dismissed` (entity `lifecycle`, source `operator`, provenance `observed`,
actor = the authenticated key) through the normal ingest path, so it is persisted and survives a restart. One event is
written per distinct (visibility scope, room) the target was seen in, so everyone who could see the agent sees it dropped
and no one learns of an agent they could not see. History is never rewritten: `events` and `replay?until=` still return
the tombstone and the earlier state.

Fold semantics: a `session.dismissed` event hides the node of its `session_id` (else every node of its `agent_id`) from
`agents`, and with it its edges, room membership, host membership and task links. Any later event of that node (in fold
order, by time) revives it, so a live agent comes back on its next heartbeat and a dead one stays gone. The fold stays
pure, order-independent and duplicate-safe; a tombstone never creates a node.

CLI: `herdr-eng agent-ops dismiss <agent_id> [--reason TEXT]` or `dismiss --session ID` (uses `HERDR_CP_URL` and
`HERDR_CP_API_KEY`/`--api-key`, which must be an operator key). The terminal map has no per-agent action list yet, so
there is no key binding.

Cursor format: `<ingested_at UTC fixed-width>~<event_id>`; treat as opaque, compare as string.

## Snapshot JSON
```json
{
 "agents": [
  {"node_id": "s1", "agent_id": "a1", "session_id": "s1", "host_id": "h1", "provider": null,
   "model": "opus",
   "model_changes": [{"from": null, "to": "opus", "at": "2026-11-12T10:00:02.000000Z", "event_id": "e2"}],
   "state": "idle", "context": {"used": 10, "limit": 100, "source": "pi"}, "room_ids": [],
   "parent_session_id": null, "compactions": 0, "drift_warnings": 0, "label": null,
   "provenance": "observed", "last_event_at": "2026-11-12T10:00:04.000000Z"},
  {"node_id": "a2", "agent_id": "a2", "session_id": null, "host_id": null, "provider": null, "model": null,
   "model_changes": [], "state": "unknown", "context": null, "room_ids": [], "parent_session_id": null,
   "compactions": 0, "drift_warnings": 0, "label": null, "provenance": "unknown", "last_event_at": null}
 ],
 "rooms": [],
 "edges": [
  {"kind": "delegation", "from": "s1", "to": "a2", "state": "accepted", "room_id": null,
   "event_id": "e4", "provenance": "observed", "task_id": "t1", "handoff_id": null,
   "updated_at": "2026-11-12T10:00:04.000000Z"}
 ],
 "tasks": [
  {"task_id": "t1", "room_id": null, "accountable_owner": null, "contributors": ["a2"],
   "reviewer": null, "updated_at": "2026-11-12T10:00:04.000000Z"}
 ],
 "cursor": "2026-11-12T10:00:04.000000Z~e4",
 "counts": {"agents": 2, "rooms": 0, "edges": 1, "tasks": 1, "events": 4},
 "truncated": false
}
```
Rules the UI can rely on:
- `node_id` = session_id if known else agent_id. Edge `from`/`to` are node_ids (or `user:<id>` for people).
- Unknown stays `null` (model, context, host_id, provider); `state` is `unknown` unless a lifecycle event says
  otherwise (`idle|working|blocked|disconnected|ended` style values). `provenance` is observed|derived|inferred|unknown.
- A model switch in the same session updates the node (`model_changes`); a new `session_id` creates a new node.
- Edge `kind`: communication|delegation|handoff|review|ownership. `tasks[].accountable_owner`, `contributors`
  and `reviewer` are separate from delegation edges: delegating never moves ownership.
- `context` is null unless a measured `context_used` was reported; `limit`/`source` may be null.
- Output is metadata only: no message text, prompts, paths or payload content; labels are sanitized.
- Bounds: 5000 events, 500 agents, 2000 edges, 200 rooms; `truncated: true` when any bound applied.

## Tests
`$VENV/bin/python -m pytest tests/control_plane/test_envelope_v2.py tests/control_plane/test_ingest.py tests/control_plane/test_handoff.py tests/control_plane/test_collab.py tests/control_plane/test_collab_a2a.py tests/control_plane/test_agent_ops_envelope.py tests/control_plane/test_agent_ops.py -q -p no:cacheprovider`
-> 235 passed. Also `test_api.py test_health_probe.py` -> 63 passed.
New: 22 envelope tests, 22 projection tests (dedupe, out-of-order shuffles, deterministic replay, room
authorization with no cross-room leak including counts, unknown stays null, model switch vs new session,
redaction, capability off 404, HTTP routes, stream resume). No mocks; real in-memory repo, CollabService and server.

## Limitations
- API keys alone see no room events.
- Handoffs yield an edge only when agent_id or session_id exists on both ends.
- Ownership edges may have a `user:<id>` endpoint.
- `model_changes` includes the initial selection with `from: null`.
- Envelope scan is in-memory: newest 500 per stream, 5000 events overall; durable-history-only deployments are not scanned.
- Stream polls once per second.
