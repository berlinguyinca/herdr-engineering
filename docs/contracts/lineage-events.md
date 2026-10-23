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

### Writer model (ownership) and admission
Two layers: `GraphProjection.admit` runs under the ledger lock BEFORE the envelope row is persisted; `apply` is a pure
commutative merge that trusts stored events. A refused event is not persisted and its sequence is not consumed (retry allowed).
- Each admitted lineage event gets a server-assigned monotonic **admission number** (`lineage_seq`), stored in the envelope row
  (`payload._envelope.lineage_seq`) and replayed. It is the cross-writer ordering and the ownership clock; wall time, source
  names and client sequences never decide either.
- A node/edge's **owner** is the actor of its first admitted event (pinned: admission numbers only grow). **Writers** = owner
  + `co_writers` (a node/edge attr, list of actor ids, max 8, settable only by `operator` principals). Operators may write
  anything (but cannot remove what does not exist: 400).
- A non-operator is refused with ONE indistinguishable `PermissionError` (HTTP 403, a single message) when it: is not a writer
  of the target; cannot see (and is not the author of) either endpoint of an edge; is not a writer of the edge's `from` node
  (so only an actor's writers can emit edges "from" it, e.g. a reviewer's reviews); removes an edge that does not exist (a
  remove never creates ownership); or tries to set `co_writers`. Producers must upsert both endpoints before the edge.
- Wildcard `edge.remove` (reviewed/validated without a binding) needs at least one existing binding and write access to every
  existing binding of the triple; it applies to adds older than itself only.
- Unowned ids are first-come: whoever creates an id first owns it (operators can override). A 403 on `node.upsert` for an
  existing id tells an authenticated ingest key that the id exists; read access reveals nothing.
- Capacity (ValueError -> 400): attrs <= 128 keys / 32 KiB TOTAL per object (per event 64 keys / 16 KiB), <= 16 distinct
  (source, actor) writers per object, <= 200k nodes, <= 1M edges, <= 10k wildcard removes, and <= 50k objects created per
  non-operator principal (fair share; the quota is rebuilt from events). Responses are capped at 2 MiB (`truncated: true`).

### Merge rules
- Every mutable field (label, state, each attr key, an edge's add, an edge's remove) is a last-writer-wins register per
  (source, actor): inside one (source, actor) the higher `source_sequence` wins. Between different writers the winners are
  compared by admission number, so a client-chosen sequence can never beat another principal, even on a shared source name.
- `visibility_scope` of a node/edge is the NARROWEST scope of any event that defined it; never widens. Only node events define
  a node; an edge to a node with no `node.upsert`/`node.state` yet creates an invisible placeholder, and edge events never
  alter a node's scope, owner or event ids. Producers must upsert endpoints.
- Edge identity is `(edge_type, from_id, to_id)`, extended with the binding for `reviewed`/`validated`. Binding = `attrs.commit`
  if present, else `attrs.plan_digest`. A `plan_digest` accompanying a commit is an ordinary attr, not a different binding.
- `removed` = the effective remove (own or wildcard, best by the cross-source rule) is newer than the effective add (later add
  resurrects; remove-before-add stays removed). A wildcard remove lives in its own store and is resolved at read time; it never
  changes existing edges' scope or owner, so merges commute. Remove details (`removal.reason`, event ids) are shown only to a
  caller who can see the remove event's own scope; others see only `removed: true`.
- All merges are commutative and idempotent, tested by shuffling every lineage event (across entities and sources) and
  comparing the projection live, after replay, and after export/import.

### Stale semantics
`stale` is computed per caller over the edges THAT caller can see (a hidden edge can never be inferred from another edge's
flag; `bindings` likewise lists only visible bindings). For a reviewed/validated edge E and another live edge O of the same type
and subject with a different binding:
Only RELATED edges are compared: O has the same `from_id` (the same reviewer, and only its writers can author edges from it) or
O's owner is a writer of E. Unrelated reviewers are never ranked against each other; they appear side by side in `bindings`.
1. O declares `attrs.supersedes` containing E's binding value => E is stale.
2. E declares it supersedes O's binding => O does not make E stale (explicit order beats arrival, so a late-arriving older-commit
   review cannot mark a newer one stale if the newer one says `supersedes`).
3. Neither declares anything => E is stale if O's add was admitted later. The server cannot know commit order: without hints,
   "newer" means "admitted later", and the UI should show `bindings` rather than trust the flag. Emitters that can deliver late
   should send `attrs.supersedes`.
Stale edges are kept and flagged.

### Session rollover
No containment edge type exists, so `attempt`/`session` nodes carry `attrs.agent_id` / `attrs.attempt_id`. A rollover is a new
`session:` node (and the old one's `node.state`); the `agent:` id and state are untouched. Closing a pane/tab emits nothing and
no Herdr code derives an agent state change from a session stop or pane close.

### Read API (viewer level, any valid key; open listener behaves as an operator)
- `GET /api/v1/graph/nodes/{node_id}` - node + its visible incident edges (capped at 200).
- `GET /api/v1/graph/nodes/{node_id}/neighborhood?depth=2&max_nodes=200&max_edges=500&direction=both|out|in&edge_types=a,b&include_removed=true`
  (alias `/subgraph`). Caps: depth 5, nodes 1000, edges 2000; out of range => 400. `truncated: true` when a cap cut visible results.
- `GET /api/v1/graph/edge?edge_type=&from_id=&to_id=[&commit=|&plan_digest=]` - edge(s) with provenance
  (`event_id`, `source`, `source_sequence` of the winning add, and `removal` when tombstoned).
- Ids contain `:`, `/`, `#`: percent-encode them.
- Hidden and missing are both 404 (writes: 403 for a non-writer). The visibility check runs for every node and edge at every hop, an edge is visible only if
  its own scope and BOTH endpoints are visible, hidden nodes are never expanded, and `truncated`/limits are computed over visible
  items only, so no counts, ids or event ids of hidden items leak. There is deliberately no total-count endpoint.

### Scope semantics today (no per-principal grant model)
| scope | who can read |
|---|---|
| `org` | any authenticated caller |
| `room`, `mission` | `operator` principals only (no room/mission membership exists to check; fail closed for viewer/ingest/agent) |
| `private` | only the object's writers (owner + operator-set `co_writers`); not operators in general |
| missing/unknown | treated as `private` at ingest; unauthenticated principals see nothing |
Limitation: `room`/`mission` cannot yet be narrowed to actual members, and an operator key is trusted for all rooms/missions.
The open (keyless loopback) listener is one shared `open-listener` principal. Principal ids are API-key digests, so a rotated key
loses access to its own private nodes.
