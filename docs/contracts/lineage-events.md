# Lineage events (herdr-federated-engineering-20261022, T02)

Carried in envelope v2 (see event-envelope-v2.md). `entity_type = "lineage"`, `entity_id` = the subject node id.
Herdr projects them into the durable graph. **Lineage is only ever created by these explicit events** - never inferred
from chat text, pane layout, or timing.

## Node ids
Stable, prefixed, opaque: `<kind>:<id>`. Kinds: `room workspace mission plan task agent attempt session tab pane handoff
thread message decision approach worktree commit pr ci review finding artifact gate issue`.
`issue` ids are repo-qualified: `issue:github.com/<owner>/<repo>#<n>`; `pr` likewise (`pr:github.com/o/r#n`).
`agent:<id>` is the LOGICAL agent and survives session rollover and host migration; `attempt:<id>` is one worker run;
`session:<id>` one model session. A rollover adds a new `session` under the same `attempt`/`agent`; it never changes ids.

## Event types
| event_type | payload | meaning |
|---|---|---|
| `node.upsert` | `{node_type, node_id, label?, state?, attrs?}` | create/update a node; idempotent, last `source_sequence` wins per field |
| `node.state` | `{node_id, state, reason?}` | state change (e.g. running, blocked, done, failed, cancelled, superseded) |
| `edge.add` | `{edge_type, from_id, to_id, attrs?}` | typed directed edge; identity = `(edge_type, from_id, to_id)`, idempotent |
| `edge.remove` | `{edge_type, from_id, to_id, reason}` | tombstone (history kept; edge hidden after) |

`edge_type` in: `spawn handoff delegation consultation dependency issue_link fork supersession reviewed produced validated`.
Direction: `spawn` parent->child; `handoff` from->to; `delegation` delegator->delegate; `consultation` asker->answerer;
`dependency` dependent->prerequisite; `issue_link` any->issue/pr; `fork` origin->new approach; `supersession` new->old;
`reviewed` reviewer->subject (attrs.commit / attrs.plan_digest REQUIRED = revision binding); `produced` producer->artifact;
`validated` validator->subject (attrs.commit REQUIRED).

## Rules
- Every event carries the envelope `source`, `source_sequence`, `visibility_scope`; graph reads must filter by it.
- `reviewed`/`validated` edges are bound to an exact commit or plan digest; a newer head makes the older edge `stale`
  (kept, flagged), never silently valid.
- Unknown `edge_type`/`node_type` are rejected, not stored.
- Closing a pane/tab never emits a state change for the agent.

## Herdr implementation notes (T02: graph projection)
Code: `herdr_engineering/control_plane/graph.py` (`GraphProjection`), wired in `ingest.py` and `repo.py`; reads in `api.py`.

### Storage decision: derived, no migration 0004
The graph is a projection over `fabric_events`. `lineage` is an envelope-only entity type (persisted, idempotent, sequenced, kept
out of the global feed and SSE). `ControlPlaneRepo.import_state` (the startup/hydrate path) replays every stored `lineage` row
through `GraphProjection.apply`. A second graph table would be a second source of truth that could drift from the event log;
every merge below is commutative and idempotent, so replay always yields the live state. Cost: restart time grows with lineage
volume (fine at current scale; a snapshot table can be added later without changing semantics). Rows that no longer validate are
skipped and counted (`events_rejected`), never fatal.

### Ingest
- `entity_type: "lineage"` requires `schema_version: 2`; v1 lineage bodies are rejected (400).
- The payload is validated BEFORE the envelope row is persisted (a bad event consumes no sequence); the projection is applied
  AFTER the row is durable (T01 ordering). Unknown `node_type`/`edge_type`/`event_type`, a `node_id` whose prefix is not a known
  kind or does not equal `node_type`, a self-loop, `issue_link` to something other than `issue`/`pr`, an `edge.remove` without
  `reason`, oversize `attrs` (16 KiB, 64 keys) are all `ValueError` (400) and not stored. `entity_id` is not cross-checked.
- Nothing is inferred. Raw lineage rows are NOT readable through `GET /api/v1/events?entity_type=lineage` (404); use the graph API.

### Merge rules
- Version of an event = `(source_sequence, source, event_id)`. Within a source that is emitter order; across sources it is only a
  deterministic tie-break.
- `label`, `state`, and each `attrs` key are last-writer-wins by version, independently (so out-of-order delivery converges).
- `visibility_scope` of a node/edge is the NARROWEST scope of any event that defined it; it never widens. Only node events
  define a node: an `edge.add` to a node with no `node.upsert`/`node.state` yet creates an invisible placeholder (no scope), and
  edge events never alter a node's scope, authors or first/last event ids. Producers must upsert endpoints.
- Edge identity is `(edge_type, from_id, to_id)`, extended with the binding `(commit, plan_digest)` for `reviewed`/`validated`
  so that a re-review at a new commit is a distinct edge (otherwise the old evidence would be overwritten, not flagged stale).
- `removed` = newest `edge.remove` version > newest `edge.add` version (a later add resurrects; remove-before-add stays removed).
  For `reviewed`/`validated`, `edge.remove` with `attrs.commit`/`attrs.plan_digest` removes that binding; without them it removes
  every binding of the triple that was added before it.
- `stale` (derived at read): a `reviewed`/`validated` edge is stale when another non-removed edge of the same type with the same
  subject (`to_id`) and a different binding has a higher add version. Stale edges are kept and flagged.
- Session rollover: there is no containment edge type, so `attempt`/`session` nodes carry `attrs.agent_id` / `attrs.attempt_id`.
  A rollover is a new `session:` node (and an old session's `node.state`); the `agent:` node id and state are untouched.
  Closing a pane/tab emits nothing and no Herdr code path derives an agent state change from a session stop or pane close.

### Read API (viewer level, any valid key; open listener behaves as an operator)
- `GET /api/v1/graph/nodes/{node_id}` - node + its visible incident edges (capped at 200).
- `GET /api/v1/graph/nodes/{node_id}/neighborhood?depth=2&max_nodes=200&max_edges=500&direction=both|out|in&edge_types=a,b&include_removed=true`
  (alias `/subgraph`). Caps: depth 5, nodes 1000, edges 2000; out of range => 400. `truncated: true` when a cap cut visible results.
- `GET /api/v1/graph/edge?edge_type=&from_id=&to_id=[&commit=|&plan_digest=]` - edge(s) with provenance
  (`event_id`, `source`, `source_sequence` of the winning add, and `removal` when tombstoned).
- Ids contain `:`, `/`, `#`: percent-encode them.
- Hidden and missing are both 404. The visibility check runs for every node and edge at every hop, an edge is visible only if
  its own scope and BOTH endpoints are visible, hidden nodes are never expanded, and `truncated`/limits are computed over visible
  items only, so no counts, ids or event ids of hidden items leak. There is deliberately no total-count endpoint.

### Scope semantics today (no per-principal grant model)
| scope | who can read |
|---|---|
| `org` | any authenticated caller |
| `room`, `mission` | `operator` principals only (no room/mission membership exists to check; fail closed for viewer/ingest/agent) |
| `private` | only the single principal (`actor.id`) that authored every event defining the node/edge; if a second author or more than 8 authors contributed, nobody |
| missing/unknown | treated as `private` at ingest; unauthenticated principals see nothing |
Limitation: `room`/`mission` cannot yet be narrowed to actual members, and an operator key is trusted for all rooms/missions.
The open (keyless loopback) listener is one shared `open-listener` principal. Principal ids are API-key digests, so a rotated key
loses access to its own private nodes.
