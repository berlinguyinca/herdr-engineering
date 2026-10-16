# Contract: AgentInspectorEvent

Extends the existing control-plane ingest envelope (`POST /api/v1/ingest`) with
one new `entity_type`, **`inspector`**, so Pi (and any other agent runtime) can
stream *observable* per-agent detail into the existing Herdr session view. It is
a projection contract, not a second event bus: transport is the existing ingest
endpoint, the existing roles (`ingest` may POST), the existing redaction
boundary, and the existing SSE/REST conventions.

```json
{
  "entity_type": "inspector",
  "entity_id": "<session_id>",         // the same id as the `session` entity (pane / worker)
  "event_type": "inspector.batch",
  "payload": { "events": [ <InspectorEvent>, ... ] }   // 1..100 per request
}
```

A single event may also be sent directly (`event_type` = the kind below,
`payload` = the event body). Batching is preferred to keep overhead low.

## InspectorEvent

| field | type | notes |
|---|---|---|
| `kind` | enum | `capabilities`, `message`, `reasoning`, `tool`, `file`, `test`, `usage`, `association`, `model`, `lifecycle` |
| `event_id` | string, optional | client idempotency key; duplicates are dropped |
| `ts` | RFC3339, optional | source time; server stamps ingest time |
| `agent_id` | string, optional | attribution: `main` (default) or a subagent name |
| `parent_agent_id` | string, optional | set for subagents |
| `sensitive` | bool, optional | emitter hint; the server also classifies independently |

Per-kind bodies (all fields optional unless stated):

- `capabilities` — `{reasoning: "none"|"summary"|"full", streaming_tools: bool, diffs: bool, usage: bool, model: str}`.
  **Reasoning is capability-dependent.** Without `reasoning != "none"` the server
  rejects `reasoning` events and the UI shows "not exposed by this model/runtime".
  Nothing is ever synthesised.
- `message` — `{message_id, role: "user"|"assistant"|"system", text, truncated}`
- `reasoning` — `{message_id, text, source: "provider_summary"|"provider_raw"}`
- `tool` — `{call_id (required), tool, phase: "start"|"update"|"end", args_preview, output, is_error, exit_code, duration_ms, truncated}`.
  `update` carries the latest *tail* chunk, not the cumulative stream.
- `file` — `{path (required), op: "read"|"write"|"edit"|"delete", call_id, diff (unified), additions, deletions}`
- `test` — `{run_id (required), command, phase: "start"|"end", passed, failed, skipped, duration_ms, failures: [{name, message, file, line}]}`
- `usage` — `{input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, context_tokens, context_window, model}`
- `association` — `{mission_id, handoff_id, handoff_from, parent_session_id, worktree, branch, pr: {number, url, repo}}`
- `model` — `{model, provider, previous_model, reason: "switch"|"handoff"|"fallback"}`
- `lifecycle` — `{phase: "started"|"restored"|"restarted"|"interrupted"|"ended", restored_from_seq}`

## Server guarantees

- **Bounded:** per-event body caps (head+tail truncation with a marker),
  per-session event/byte rings, global session cap with eviction of ended
  sessions first. Limits are configurable and have hard ceilings.
- **Redacted at the boundary** with the existing `control_plane.redact` rules
  before storage; events whose text changed are flagged `redacted: true`.
- **Access-controlled:** reasoning text and `sensitive` tool output/diff bodies
  are returned only to roles allowed by `inspector.access` config (default:
  `operator`). Other roles receive metadata with the body replaced by a
  `{"withheld": true}` marker.
- **Idempotent and ordered:** the server assigns a per-session monotonic `seq`;
  retries with the same `event_id` are no-ops; out-of-order `ts` is allowed.
- **Gaps are explicit:** if a ring evicts events or a slow SSE client falls
  behind, responses/streams carry `dropped`/`gap` counters. Missing telemetry is
  shown as missing, never interpolated.
- **Tool calls and test runs are single mutable entries.** `tool start` creates
  one ring entry (one `seq`); `update`/`end` mutate it in place and bump its
  `rev`, so streaming status never multiplies history. A tool that is still
  `running` when the agent reports `lifecycle: restarted|interrupted|ended` is
  marked `interrupted`; an `update`/`end` with no prior `start` is stored with
  `incomplete: true`. Success is never inferred.
- **Usage** `input/output/reasoning/cache_read_tokens` are *per-request* deltas
  (the server sums them); `context_tokens`/`context_window` are the current value.
- **Durability:** only low-volume summary kinds (`capabilities`, `association`,
  `model`, `lifecycle`, `test` results with up to 20 failure names) and
  preferences are written to the existing durable sink (as `inspector` events,
  never into the in-memory feed or the global SSE stream). Bodies live only in
  the bounded ring and do not survive a control-plane restart; emitters should
  send `lifecycle: restored` plus fresh `association`/`capabilities` when they
  reconnect.

## Read API (viewer role or above)

- `GET /api/v1/sessions/{id}/inspector?view=all|conversation|reasoning|tools|files|tests|usage&q=&agent_id=&status=&since_rev=&before_seq=&limit=` (newest first; `since_rev` returns only entries created *or mutated* after that revision)
- `GET /api/v1/sessions/{id}/inspector/events/{seq}` — full (still bounded) body
- `GET /api/v1/sessions/{id}/inspector/stream` — SSE, per-session (frames: `connected`, `event` {full entry}, `gap` {dropped} → resync with `since_rev`)
- `GET|POST /api/v1/inspector/preferences`, `GET|POST /api/v1/sessions/{id}/inspector/preferences` (POST: operator)
- `GET /api/v1/inspector/health`

See `docs/specs/0200-agent-inspector.md` for configuration, rollback and operations.
