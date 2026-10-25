# Event envelope v2 (herdr-federated-engineering-20261022, T01)

Single canonical envelope for everything Pi (execution authority) emits and Herdr (`fabric_events`) stores.
It extends the existing `Event` / `POST /api/v1/ingest` contract; v1 bodies keep working unchanged.

## Fields
| Field | Set by | Rule |
|---|---|---|
| `schema_version` | emitter | `2` for this contract. `1`/absent = legacy. `>2` is rejected (fail closed). |
| `event_id` | emitter | Unique per authenticated principal (migration 0005), stable across retries (an emitter retry reuses the id). |
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
2. **Sequence**: the sequence domain is `(actor.id, source)` (the authenticated principal plus the `source` string), so two principals may use the same `source` without burning each other's sequences, gaps or freshness. `(source, source_sequence)` already seen IN THE SAME DOMAIN with a different `event_id` => `409`. A jump greater than 1 records a **gap** in per-source state (`last_sequence`, `last_seen_at`, `gaps: [{from,to,seen_at}]`) and accepts the event; a later arrival inside a gap closes it (out-of-order replay is allowed).
3. **Freshness/provenance**: per-source state is queryable; a source with no events within its freshness window is reported `stale`, never `healthy` by default.
4. **Durability**: an append failure is an error, never reported as a duplicate. Replay (`read_stream`) returns events with all v2 fields.
5. **No fabrication**: the server never synthesizes progress, spawn edges or heartbeats.
6. **Authorization**: ingest requires the existing `ingest`/`agent`/`operator` role; provider-owned entity types stay blocked from generic ingest.

## Emitter rules (Pi)
- One envelope builder shared by ledger, platform, lifecycle-telemetry and inspector emitters.
- `source_sequence` allocated from a persisted counter (atomic, crash-safe); never reused.
- Emitting is best-effort and must not block or fail local execution; a durable local outbox keeps events for replay when Herdr is unreachable. Pi works with no Herdr at all.

## Herdr implementation notes (resolved ambiguities)
- A v2 body must carry `event_id`, `source` (no `herdr` default: the source is the sequence domain) and `source_sequence`.
- The first event seen from a source sets its baseline; it never opens a gap. A later arrival inside a gap removes that sequence from the gap (the gap splits or closes).
- Freshness window defaults to 300 s (`SourceLedger.freshness_seconds`); `GET /api/v1/ingest/sources` lists `status: healthy|stale` (all domains to operator keys, only the caller's own to any other key; T16 M4). `?scope=all` is the explicit operator-only full listing. A key keeps at most 32 domains and cannot evict another key's (T16 M6). Ingest accepts an allow-list of entity types only (T16 H1).
- `actor.id` is `key:<12-hex sha256 digest of the API key>`; `kind` is the key's role. An open (keyless loopback) listener reports `open-listener`.
- No per-principal visibility grant exists yet, so the server only validates the enum (unknown => `private`); it does not narrow further.
- Idempotency compares emitter-controlled content (not `actor`/`received_at`). The content digest is stored in the envelope row (`payload._envelope.content_sha256`) so a restart-rebuilt ledger still detects same-id/different-content.
- Ordering guarantee: the envelope row is persisted FIRST, then the typed handler runs. A persistence failure applies nothing and leaves the ledger untouched (retry allowed). Known handler validation errors are checked before persisting. If a handler still fails after the row is durable, the sequence stays consumed, the error is reported once, and a retry is acknowledged as `duplicate: true` without re-applying.
- Every acknowledged v2 envelope has a `fabric_events` row. For `host`/`session`/`mission`/`service`/`inspector`/`mission_activity` it is an envelope-only row: kept out of the global feed and out of state reconstruction, and for inspector/mission_activity it carries no payload (their stores redact per field).
- Lost acknowledgement: if the durable insert already holds the `event_id` with the same `(source, source_sequence)`, the retry is acknowledged as a duplicate; a different identity is a 409.
- Bounds: `source_sequence` <= 2^63-1; at most 64 gaps per source (oldest collapse into a coarser range, `gaps_truncated: true`); at most 1024 tracked sources (stalest evicted); 100k in-memory event index (database unique indexes are the backstop).
- The durable write happens while holding the ledger lock (simple and atomic; serialises v2 ingests).
- Sequence domain = (principal, source) (migration `0004_sequence_domain_per_principal.sql`: unique index on `(actor->>'id', source, source_sequence)`, `fabric_event_sources` keyed by `(source_actor, source)`). `GET /api/v1/ingest/sources` lists one entry per domain with an `actor` field (the key digest id). Until 0004 is applied the old global index still enforces the stricter rule. `event_id` is unique per principal too (migration `0005_event_id_per_actor.sql`: unique index on `(actor->>'id', event_id)`, the global primary key is dropped), so another key cannot squat a guessable id; idempotency, content-hash conflicts, the durable duplicate check (`EventStore.identity(event_id, actor_id)`) and the in-memory ledger are all keyed `(actor, event_id)`. Until 0005 is applied the old global primary key still enforces the stricter rule (inserts use a bare `ON CONFLICT DO NOTHING`, so the new code works before and after; cross-principal reuse then answers `409` naming the event_id and migration 0005). **Rollout order: deploy the code first, then apply 0005** - the previous release's `ON CONFLICT (event_id)` fails once the primary key is gone. Rolling the code back after 0005 needs the down-step in `docs/operations/event-id-per-actor-rollout.md`.
