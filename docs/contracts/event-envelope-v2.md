# Event envelope v2 (herdr-federated-engineering-20261022, T01)

Single canonical envelope for everything Pi (execution authority) emits and Herdr (`fabric_events`) stores.
It extends the existing `Event` / `POST /api/v1/ingest` contract; v1 bodies keep working unchanged.

## Fields
| Field | Set by | Rule |
|---|---|---|
| `schema_version` | emitter | `2` for this contract. `1`/absent = legacy. `>2` is rejected (fail closed). |
| `event_id` | emitter | Globally unique, stable across retries (an emitter retry reuses the id). |
| `source` | emitter | Existing `[a-z][a-z0-9_-]{0,31}` rule. Identifies the sequence domain. |
| `source_sequence` | emitter | Integer >= 0, strictly increasing per `source`, assigned at emit time and persisted by the emitter so it survives restart. |
| `occurred_at` | emitter | RFC 3339 UTC; stored as `source_timestamp`. |
| `received_at` | **server** | Always overwritten by Herdr; stored as `ingest_timestamp`. |
| `org_id` `project_id` `room_id` `workspace_id` `mission_id` `task_id` `agent_id` | emitter | Optional strings. Never inferred from chat text. |
| `correlation_id` | emitter | Groups a mission/flow. |
| `causation_id` | emitter | `event_id` of the event that caused this one. Lineage (spawn/handoff/...) is only ever derived from explicit causation or typed events, never guessed. |
| `actor` | **server** | `{kind, id, authenticated_by}` taken from the authenticated principal. A client-supplied `actor` is kept only as `claimed_actor` in the payload envelope metadata and never trusted. |
| `visibility_scope` | emitter, server-clamped | `private` \| `mission` \| `room` \| `org`. Missing/unknown => `private` (fail closed). Server may narrow, never widen, beyond the principal's grant. |
| `evidence_refs` | emitter | List of `{kind, uri, digest?, revision?}`; revision/digest bind evidence to an exact commit or plan digest. |
| `entity_type` `entity_id` `event_type` `payload` | emitter | Unchanged from v1. |

## Server rules
1. **Idempotency**: same `event_id` + identical content => acknowledged, not re-applied (`duplicate: true`). Same `event_id` + different content => `409 conflict`, original preserved.
2. **Sequence**: `(source, source_sequence)` already seen with a different `event_id` => `409`. A jump greater than 1 records a **gap** in per-source state (`last_sequence`, `last_seen_at`, `gaps: [{from,to,seen_at}]`) and accepts the event; a later arrival inside a gap closes it (out-of-order replay is allowed).
3. **Freshness/provenance**: per-source state is queryable; a source with no events within its freshness window is reported `stale`, never `healthy` by default.
4. **Durability**: an append failure is an error, never reported as a duplicate. Replay (`read_stream`) returns events with all v2 fields.
5. **No fabrication**: the server never synthesizes progress, spawn edges or heartbeats.
6. **Authorization**: ingest requires the existing `ingest`/`agent`/`operator` role; provider-owned entity types stay blocked from generic ingest.

## Emitter rules (Pi)
- One envelope builder shared by ledger, platform, lifecycle-telemetry and inspector emitters.
- `source_sequence` allocated from a persisted counter (atomic, crash-safe); never reused.
- Emitting is best-effort and must not block or fail local execution; a durable local outbox keeps events for replay when Herdr is unreachable. Pi works with no Herdr at all.
