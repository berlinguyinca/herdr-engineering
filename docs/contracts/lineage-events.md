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

### Node kinds: COMMONS vs OWNED
- **COMMONS** (`commit pr issue ci artifact`): shared identities several emitters legitimately mention. Anyone with an
  ingest/agent key may create one, and write its fields (`label`/`state`/`attrs`): fields are per-`(source, actor)` registers
  kept separately and attributed. A non-writer's write is admitted as `foreign`: it never touches the node's owner or
  first/last event ids. Only an operator moderates (operators may write anything, including `co_writers`).
  - **Primary value** of a commons field: an operator's value if any operator wrote it, else the node owner's, else the
    earliest writer's. Everyone else's values are `contributions` (<= 5 per field, latest admission first), never the primary,
    so overwriting a PR's label/state/url cannot replace what the creator said. A node view carries `fields.<name>`
    provenance `{value, writer, source, event_id, admission, admitted_at}` and, for commons, `contributions.<name>`.
    `source` is the creator's source. Attr keys follow the same rule; attr budgets for commons are PER WRITER under a hard
    per-object cap (1024 keys / 256 KiB) so a writer cannot fill a node and lock others out.
  - **Scope of a commons node is the WIDEST scope any non-operator wrote** (an operator who has written decides, using their
    narrowest), so pre-creating a commons node as `private` cannot hide it. Owned kinds keep the narrowest scope.
  - Edges whose `from` node is commons and whose type is unbound (`produced`, `issue_link`, ...) are kept PER WRITER (at most 8
    writers per triple); views carry `writer`. `edge.remove` removes the caller's own instance; `attrs.writer` names another
    writer's instance, which the owner of either endpoint or an operator may tombstone.
  - `node.state` on a missing commons node creates it (like an upsert), so hidden and missing commons nodes answer alike.
  - Payload keys starting with `_` are reserved and rejected (400); the admission number/mode are server-set top-level fields
    of the stored row and are never read from the client payload.
- **OWNED** (every other kind: `agent attempt session mission plan task handoff pane tab thread worktree room workspace review
  message decision approach finding gate`): the creator owns it; only its writers may change it.
- **Ensure**: a `node.upsert` with no `label`/`state`/`attrs` (and an `edge.add` with no attrs beyond the binding) on an
  existing object you do not own is accepted and acknowledged as a `noop` - persisted for idempotency but applied as nothing
  (so it cannot resurrect a removed edge, move scope or ownership). This is what Pi sends for every endpoint before every edge.
  An ensure of a missing node creates it, so the answer is `ok` whether or not the object exists (no existence oracle).

### Writer model (ownership) and admission
Two layers: `GraphProjection.admit` runs under the ledger lock BEFORE the envelope row is persisted; `apply` is a pure
commutative merge that trusts stored events. A refused event is not persisted and its sequence is not consumed (retry allowed).
- Each admitted lineage event gets a server-assigned monotonic **admission number** (`lineage_seq`) and an admission **mode**
  (`normal` | `foreign` | `noop`), both stored in the envelope row's server metadata (`payload._envelope`, never
  client-controlled) and replayed. They are the cross-writer
  ordering and the ownership clock; wall time, source names and client sequences decide neither. Rebuild applies rows in
  admission order; the result is order-independent anyway (shuffle-tested).
- A node/edge's **owner** is the actor of its FIRST admitted event, whatever its role (so an operator-created object is
  operator-owned until an operator sets `co_writers`). Writers = owner + `co_writers` (attr, list of actor ids, max 8,
  operator-only). Per-principal object quota is charged to the pinned owner, so it is identical live and after rebuild.
- A non-operator is refused with ONE indistinguishable `PermissionError` - HTTP 403 with body
  `{"error": "not permitted to modify this lineage object", "reason": "lineage_forbidden"}` (an auth failure has no `reason`,
  so emitters should dead-letter `lineage_forbidden`, not treat it as a credentials problem) - when it: changes (non-ensure) an
  OWNED object it is not a writer of; changes an edge it does not own; cannot see (and is not the author of) an endpoint of
  an edge; is not a writer of the edge's `from` node (exception: a COMMONS `from` node for an unbound edge type, so
  `produced`/`issue_link` between shared nodes work; `reviewed`/`validated` always need the `from` node's writer); removes an
  edge that does not exist (a remove never creates ownership); `node.state` on a missing node; sets `co_writers`; or is at its
  object quota (identical for existing and missing targets).
- Wildcard `edge.remove` needs at least one existing binding and write access to every existing binding of the triple; it
  applies to adds older than itself only.
- Unowned ids are first-come: for OWNED kinds whoever creates an id first owns it (a squatter can pre-create `agent:<id>`;
  operators can override). A 403 on a changing write to an existing id tells an authenticated key that the id exists; reads
  reveal nothing.
- Capacity (ValueError -> 400): attrs <= 128 keys / 32 KiB TOTAL per object (per event 64 keys / 16 KiB), <= 64 distinct
  `(source, actor)` writers per object and <= 4 per actor, <= 200k nodes (non-operators may fill 80%), <= 1M edges, <= 10k
  wildcard removes, <= 25k objects owned per non-operator principal. Responses are capped at 2 MiB.

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
- `GET /api/v1/graph/nodes/{node_id}[?edge_type=&from_id=&limit=<=200&offset=]` - node + a page of its visible incident edges.
  The caller's own edges come first and are exempt from the page cut; the rest are newest admission first. `edges_omitted`
  counts visible edges not returned (never hidden ones); `next_offset` pages on; at most 3000 incident edges are examined per
  read (`scan_exhausted`). Neighborhood traversal follows own edges first, then newest first (500 examined per node).
- `GET /api/v1/graph/nodes/{node_id}/neighborhood?depth=2&max_nodes=200&max_edges=500&direction=both|out|in&edge_types=a,b&include_removed=true`
  (alias `/subgraph`). Caps: depth 5, nodes 1000, edges 2000; out of range => 400. `truncated: true` when a cap cut visible results.
- `GET /api/v1/graph/edge?edge_type=&from_id=&to_id=[&commit=|&plan_digest=]` - edge(s) with provenance
  (`event_id`, `source`, `source_sequence` of the winning add, and `removal` when tombstoned). `bindings` lists at most 20
  distinct visible bindings of the subject (`bindings_truncated`); reads index a subject's reviews once per request, so cost is
  linear in the subject's visible reviews, not quadratic.
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
