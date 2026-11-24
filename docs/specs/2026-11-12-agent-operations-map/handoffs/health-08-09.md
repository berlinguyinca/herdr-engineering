# Backend handoff: slices 08 + 09 (context health, drift, fleet, reconnect)

Branch `feat/agent-ops-health`. Code commit: `da33f84a` (agent_ops.py + tests/control_plane/test_agent_ops_health.py).
All new keys are additive and optional; existing keys and shapes are unchanged.

## New snapshot keys
- `agents[].context_health`: `{fraction, level, source, measured}`. `fraction = used/limit` (null when used or a positive
  limit is missing); `level` ok (<0.70) | elevated (>=0.70 amber) | high (>=0.90 red) | critical (>=1.0) | unknown.
  Unknown stays unknown (never 0); `measured` is true only when a fraction could be computed.
- `agents[].drift`: list of `{kind, provenance (derived|inferred), evidence_event_ids, task_id, heuristic: true, note}`.
  Kinds: `reported_drift_warning` (from drift.warning events, inferred if any was inferred), `repeated_tool_failures`
  (>=3 tool.failed with no tool.ended between), `handoff_stalled` / `handoff_failed` (see below). Also `tasks[].drift`.
  Notes state it is a heuristic, never a proven policy violation. No raw content. `drift_warnings` count kept.
- `rooms[].context_health`, `hosts[].context_health`, `health.context`: `{measured_count, total, average_fraction, level}`,
  averaged over measured agents only.
- `hosts[]`: `{host_id, agents (node ids), state connected|disconnected|unknown, last_seen, connected true|false|null, reconnects, context_health}`.
  `counts.hosts` added.
- `edges[]` (handoff kind) may gain `interrupted_by` (event id) and state `stalled` / `failed`.

## Behaviour
- Host-level event: `agent.disconnected` / `agent.reconnected` with `host_id` and NO agent_id/session_id. Disconnect sets host
  connected=false and every agent on that host to state `disconnected` (kept, history intact); reconnect restores each agent's
  latest pre-disconnect state (events during the outage update the remembered state, not the visible one) and counts
  `reconnects` only when it follows a disconnect. Agent-level disconnect/reconnect (with agent/session) only moves that agent.
  `connected` is null until a host-level event is observed (host metadata exists only as `host_id` on events).
- Handoff in flight (edge state planned/accepted) whose endpoint disconnects -> `stalled`; endpoint `session.ended` -> `failed`.
  Completed/other states are untouched. Not auto-reverted on reconnect (a fresh handoff.state event is needed).
- Fold remains deterministic: shuffles + duplicates identical; replay `until` gives historical hosts/health.
- Authorization precedes the fold: hosts, health and counts for unreadable rooms never appear.

## Example
```json
{"agents":[{"node_id":"s1","state":"disconnected","host_id":"h1","context":{"used":90,"limit":100,"source":"pi"},
  "context_health":{"fraction":0.9,"level":"high","source":"pi","measured":true},
  "drift":[{"kind":"handoff_stalled","provenance":"derived","evidence_event_ids":["e5","e3"],"task_id":"t1","heuristic":true,
            "note":"Heuristic signal derived from event metadata; not a proven policy violation."}]}],
 "hosts":[{"host_id":"h1","agents":["s1"],"state":"disconnected","last_seen":"2026-11-12T10:00:05.000000Z","connected":false,
           "reconnects":0,"context_health":{"measured_count":1,"total":1,"average_fraction":0.9,"level":"high"}}],
 "health":{"context":{"measured_count":1,"total":1,"average_fraction":0.9,"level":"high"}}}
```

## Tests
17 new in tests/control_plane/test_agent_ops_health.py (thresholds, unknown, measured-only aggregate, drift, host disconnect/reconnect,
out-of-order + duplicates, replay, crash mid-handoff, authorization no-leak, ingest end to end).
`pytest test_agent_ops_health test_agent_ops test_agent_ops_envelope test_envelope_v2 test_collab test_api` -> 219 passed.

## Limitations
- No scope-expansion or time-based stall heuristic (no grounded event fields / fold has no clock).
- Host connectivity only from host-level events; no separate host registry exists to reuse (agent_hosts is auth config).
- Agents that learn their host_id after a host disconnect are not retro-marked.
- Stalled/failed handoff edges are not revived by reconnect.
