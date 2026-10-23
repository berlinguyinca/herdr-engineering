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
  - **Ownership**: a commons node's owner is the writer of the first FIELD-bearing admitted write (label, state, attrs or
    `node.state`). An empty "ensure" never creates ownership (it only creates the shell), so pre-ensuring an id gains nothing.
  - **Primary value** of a commons field: an operator's value if any operator wrote it, else the node owner's, else the
    writer who wrote THAT FIELD first (their first write on the key, so it is stable when they update) - chosen among the
    values the READER can see; the owner a reader applies is the earliest field-writer among the writers visible to them, so
    hidden writers never influence what a reader sees. Everyone else's values are `contributions` (<= 5 per
    field, one per distinct writer; operator first, then the owner, then the caller's own, then newest admission), never the
    primary, so overwriting a PR's label/state/url cannot replace what the creator said and spam principals cannot push the
    owner's entry out. A node view carries `fields.<name>` provenance `{value, writer, source, event_id, admission,
    admitted_at}` and, for commons, `contributions.<name>`. Attr budgets for commons are PER WRITER under a hard per-object
    cap (1024 keys / 256 KiB) so a writer cannot fill a node and lock others out.
  - **Visibility is per writer and per field.** Every value carries the scope its writer gave it and is visible only
    through THAT writer (`private` = that writer alone, `mission`/`room` = operators, `org` = any authenticated caller).
    A commons node EXISTS for a reader if any of its writers is visible to them (so pre-creating it as `private` cannot
    hide it from its real emitters); the shell (`id`, `node_type`, `visibility_scope` = the widest scope among the writers
    visible to the reader) is shown, but ONLY the fields of visible writers: a widened node reveals nothing of another
    writer's private label/state/attrs/contributions, and `first_event_id`/`last_event_id`/`source` are derived from the visible
    values alone. Neighborhood, binding, stale and truncation figures are computed over visible items only. Owned kinds are
    unchanged: the node is as private as the narrowest scope any admitted event carried.
  - Edges whose `from` node is commons and whose type is unbound (`produced`, `issue_link`, ...) are kept PER WRITER (at most 8
    LIVE writers per triple, 64 records including tombstoned ones; operators are exempt from the live cap); views carry
    `writer`. `edge.remove` removes the caller's own instance. `attrs.writer` names another writer's instance, which only an
    operator or the owner of an OWNED-kind endpoint may tombstone - a commons endpoint (whose "owner" anyone can become)
    confers no such authority.
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

### Scope semantics (T08: room and mission membership)
A scope is a LABEL derived from the stored envelope fields of the event that carried it: `private`, `org`, a bare `room` /
`mission` (no id on the envelope), or a contextual `(room, room_id, -, workspaces)` / `(mission, mission_id, room_id, workspaces)`
when the envelope names `room_id` (resp. `room_id` AND `mission_id`) - so live ingest and rebuild read the same stored columns.
| label | who can read |
|---|---|
| `org` | any authenticated fleet KEY (keyed API). **Never a signed-in person**: the HCN route does not read `org` lineage |
| `room` + `room_id` | operator keys; a signed-in, approved NON-GUEST member of that room (expired membership does not count) |
| `mission` + `room_id` + `mission_id` | operator keys; a person on the member list of the T07 `link_mission` link **in that same room** (`room_id`), while still a non-guest member of it. Another room linking the same mission id gains nothing. A mission event without `room_id` is the bare, operator-only scope |
| either, plus `workspace_id` | as above AND that room holds a link to the workspace granting `status`, `tabs` or `panes` (revoking the grant, or unlinking, hides it). Events of one object that name several workspaces need a link to EACH (at most 8, then the bare label) |
| bare `room` / `mission` | operator keys only (fail closed; no member can satisfy a label that names nothing) |
| `private` | only the object's writers (owner + operator-set `co_writers`) |
| missing/unknown | treated as `private` at ingest; unauthenticated principals see nothing |
- **Membership is evaluated at read time**, per request, from a snapshot taken under the HCN service lock
  (`CollabService.graph_access`). Grant, revoke, role change to guest, member removal, mission unlink and link-grant
  changes are effective on the next read. Nothing about membership is stored in the graph, so rebuild == live as long as
  the HCN log and the lineage rows are both replayed (tested for every reader across grant and revoke).
- **Who is a reader.** Only people (HCN user principals). Agents, guests, unapproved people and people with no non-guest room
  membership get one 404 for the whole route. Room access still never implies workspace terminal access.
- **Front doors.** `GET /api/v1/collab/graph/nodes/{id}`, `.../nodes/{id}/neighborhood` (alias `subgraph`),
  `.../graph/edge` and the listing `.../graph/nodes?node_type=&edge_type=&limit=&room_id=` take the same parameters (the listing REQUIRES `room_id`: it is always one room's view and costs the size of that room's context, via an index kept at apply time in live and rebuild alike; the response carries `truncated_reason`: `limit`, `scan_budget` or `edge_budget`, never a count of hidden nodes) as the keyed `/api/v1/graph/...` routes and use the same `read_graph` router.
  They exist only when the collab service runs in the combined server (not `collab_only`, which by design never sees fabric
  data): in the isolated process the route is a plain 404. The keyed API still sees `room`/`mission` as operator-only.
- **Meet rules** (a node/edge keeps the NARROWEST label of any event that defined it, never widening). Labels form a
  semilattice: `private` < bare `mission` < bare `room` < contextual room `(room, id, workspaces)`; bare `mission` <
  contextual mission; `org` on top for fleet keys. Two labels of the SAME room/mission union their workspace gates (more
  gates = narrower), so a later event with no or a different workspace never makes the node vanish for a room that is
  linked to all of them. Two DIFFERENT contexts (another room, a mission vs a room) meet at the bare label, which only
  operators read: a producer that re-scopes one node into a second room hides it from both audiences rather than leaking it
  into either. Commutativity, associativity and idempotence are tested over the whole label universe, together with
  "the meet is never visible to someone who could not see both inputs" (`org` aside: it is fleet data, not people data).
  Commons nodes keep per-writer labels (at most 4 per writer) and every field stays gated by its own writer's label.
- **Key scopes (who may address a room or mission).** Operator YAML, no environment variables:
  ```yaml
  control_plane:
    lineage_key_scopes:            # key string exactly as in api_keys -> what it may address
      "<ingest key>": {rooms: ["<room id>"], missions: ["<mission id>"]}
  ```
  Enforced at ingest before anything is stored or sequenced. A non-operator key may write `room` scope only with a listed
  `room_id`, and `mission` scope only with a listed `room_id` AND a listed `mission_id`; otherwise it is refused with the
  usual 403 `lineage_forbidden`. **A key with no entry may write only `org`/`private` scopes (fail closed; this is the new
  default).** `"*"` in a list is an explicit opt-in for every id and for bare id-less scopes (the old behaviour; for trusted
  fleet keys only). Operator keys are not restricted, and an entry naming an operator-role key is
  refused at startup (it would silently do nothing; the error never echoes the key). Entries must name a configured key;
  malformed entries stop startup. **The lists are a cross-product, not pairs:** a key listed with `rooms: [A, B]` and
  `missions: [M]` may write mission `M` in room `A` AND in room `B`. Keep the lists narrow (one room and its missions per
  key, or one key per room) so a mission id is never allowed in a room that did not link it.
- **Trust.** The room/mission/workspace named on an event is whatever the (authenticated) ingest key put in its envelope, now
  limited to what the operator listed for that key (above). Residue:
  ingest keys are fleet credentials, and they could already publish `org` lineage that every signed-in key reads. The new
  capability is only that such a key can address content to a specific room's members; it shows attributed (`writer`,
  `source`) and cannot change what any other writer sees, and a room or mission that does not exist (or is not linked)
  matches nobody. Principal ids are API-key digests, so a rotated key loses access to its own private nodes.
- The open (keyless loopback) listener is still one shared `open-listener` operator principal.

### Tabs and panes (T08)
Read-only panel "Tabs" in the room page (`packages/collab-tabs.js`; model `buildTabsPanes`, renderer `renderTabsPanes`).
It reads `GET /api/v1/collab/graph/nodes?node_type=agent&edge_type=spawn&room_id=<room>` (plus `tab` and `pane` listings), the
agents this person may see in THIS room (room scope plus the room's linked missions they belong to, with the visible `spawn`
edges between them), so it can only ever show what membership already allows. The listing is bounded (500 nodes, 3000 ids
examined) and never counts hidden nodes. Pi's lineage emitter defines no `workspace:`/`mission:` root node, which is why the
panel lists by type instead of walking from a root.
- An `agent:` node with no visible incoming `spawn` edge from another agent is a **primary agent -> tab**. An agent with one is
  a **subagent -> pane** under the nearest tab ancestor (depth shown). Several parents: the smallest id wins (deterministic).
  A spawn cycle is cut (the cyclic edge is ignored). Nothing is inferred from chat, names, order or timing.
- `attrs.presentation = "tab"` (long-lived) or `attrs.external = true` (coworker / remote A2A agent; optional
  `attrs.reported_by`) gives an agent its OWN tab and keeps its spawn lineage. An external agent's tab is virtual: its
  status is whatever the remote reported, it is labelled "Remote" and never has a terminal control.
- **Closing a pane never kills an agent.** Surface state lives on a `tab:` / `pane:` node whose `attrs.agent_id` names the
  agent; `state: closed` only adds a "pane closed" note. The agent keeps its own reported state and stays listed.
- Workspace-link grants refine the view: when an agent's `attrs.workspace_id` is set and linked, it is shown as a tab only with the
  `tabs` grant and as a pane only with `panes`; otherwise it is counted ("N more are not shared") without names.
- No terminal control exists on this page (click-to-inspect is T09 and needs `authorize_workspace`). `can_terminal` in the
  model is only the server's per-link answer.
- Accessibility: `tablist`/`tab`/`tabpanel` roles, roving tabindex, Arrow/Home/End keys (`nextTab`), no animation.

### T08 rollout and rollback
- Code-only, no migration. Deploy order: server first (new read routes and label handling), then the web files
  (`collab-tabs.js` is served from the static map in `api.py`). Emitters need no change to keep today's behaviour; to make
  lineage visible to a room, a producer sets envelope `room_id` (and `visibility_scope: "room"`), or `room_id` + `mission_id` with
  `visibility_scope: "mission"`, plus `workspace_id` when the workspace link should gate it, **and the operator adds the key to
  `control_plane.lineage_key_scopes`** (without an entry the key can no longer write room/mission scope: existing deployments
  that did so get 403 `lineage_forbidden` until listed, or until `"*"` is set to keep the old behaviour). Pi needs
  `visibility: mission`, a key allowed for its mission and room, and the room id on its envelope (Pi does not send `room_id` today).
- Rollback: revert the PR. Stored events are untouched (labels are derived at read time), so the graph rebuilds exactly as
  before: contextual scopes become bare `room`/`mission` again (operator-only), i.e. members lose access, nothing leaks.
  A web-only rollback just removes the Tabs panel.

### Known limitations (graph, T02)
- **Owned-kind squat**: for owned kinds (`agent`, `task`, ...) whoever creates an id first owns it; operators can override.
  A 403 on a changing write tells an authenticated key the id exists (reads reveal nothing).
- **Private squat blocks edges**: a writer's `private` field write to a commons node (e.g. `pr:42`) makes another emitter's
  empty ensure a no-op and its following `edge.add` a 403 (the edge needs an endpoint the caller can see); Pi swallows it, so the
  Pi side counts/dead-letters `lineage_forbidden`. Resolution is operator-side (tombstone/override) today.
- **Commons primary rule**: the primary value is the operator's, else the owner's, else the first writer of that field; an
  owner who got there first holds it until an operator corrects it. Others' values are attributed `contributions`.
- **Capacity errors** on a commons object (`lineage object limit reached`) are deliberately generic: they depend on data of
  writers the caller cannot see. Exact parity is not attempted for owned objects (their caps count only the owner's data).
- **Caps at scale** (200k nodes, 1M edges, 25k objects per principal) are enforced but not load-tested at that size; tombstoned
  edge instances are not compacted (bounded by 64 records per triple and the global edge cap).
- **Membership resolution (T08)** exists only on the HCN-authenticated route and only in the combined server; the keyed
  `/api/v1/graph` API still treats `room`/`mission` as operator-only. Agents and guests read no graph.
- **Node ids are one namespace.** Two keys allowed for different rooms can still collide on an owned id such as `agent:main`
  (first creator owns it). Emitters should include a room/mission-specific part in agent ids; not enforced.
- **Pi (pi-t02 `src/runtime/herdr/lineage.ts`, read for this PR)** puts `mission_id` (and task/agent/workspace ids when it has
  them) on the envelope and uses ONE configured `visibility_scope` for the whole emitter; it never sets `room_id`. Since a mission
  is bound to the room that linked it, Pi lineage becomes member-readable only with `visibility: mission`, a T07 mission link that
  lists the people, a key allowed for that room and mission, AND a `room_id` on the envelope, which Pi does not send today
  (until then it stays operator-only and a key without `"*"` cannot even write it: **unfixed on the Pi side**). It emits `agent`/`attempt`/`session`/`handoff`/`artifact` nodes and agent->agent `spawn` edges
  (so tabs and panes work from `mission` scope), but no `tab:`/`pane:` surface nodes, so "pane closed" notes stay empty until
  an emitter adds them (with `attrs.agent_id`). Pi does not put `workspace_id` on agent attrs, so link-grant filtering of
  tabs/panes does not apply to Pi data (the server-side workspace gate still does when `workspace_id` is on the envelope).
- **Breaking: the listing needs `room_id`.** `GET /api/v1/collab/graph/nodes?node_type=...` without `room_id` is now 400
  (it was briefly an all-rooms listing). The keyed `/api/v1/graph/nodes` listing is unchanged. The per-room index is walked in
  admission order (no sort of the whole set), bounded by `SCAN_BUDGET`; a key allowed for a room can still fill that room's
  own budget with its own nodes, which `truncated_reason: scan_budget` reports. Other rooms are unaffected.
- **Stored pre-T08 mission events** that carry `mission_id` but no `room_id` have no room to bind to, so after a rebuild
  they are the bare operator-only scope (fail closed; nothing leaks, members lose sight of them). To restore member access,
  re-emit them with `room_id` (events are idempotent per `event_id`, so emit new event ids; the narrowest scope of an
  object's events wins, so an object whose OLD events lack `room_id` stays operator-only until an operator re-creates it
  under a new id or the old events are retired).
- **Large rooms**: the Tabs panel makes three listing reads (agents, tabs, panes) at most every 8 s while open; not load-tested.

## Lineage views and replay (T09)
Read-only workbench for ONE room: a lineage graph, a Gantt timeline, a replay scrubber and a node inspector. It extends the
T08 room page (`packages/collab-lineage.js`, sub-tab "Lineage" in `collab-view.js`) and the T08 collab read routes. No new
dashboard, service, event store or table: **no migration**. The projection already derives from `fabric_events`; T09 keeps the
applied events (`GraphProjection._log_ctx`: one list per room/mission context a scope names, bounded, oldest evicted) in memory next to
the graph, refilled by the same rebuild that refills the graph, so a restart loses nothing. A snapshot table can be added later
without changing semantics (same argument as T02).

### Routes (collab namespace, HCN-authenticated, `room_id` REQUIRED, same access snapshot and 404 as `graph/nodes`)
| route | returns |
|---|---|
| `GET /api/v1/collab/graph/lineage?room_id=&[step=\|at=]&limit=&offset=&node_types=&edge_types=` | `as_of`, `nodes` (every node type of the room view), `edges` among them, `total_nodes`, `next_offset`, `truncated`/`truncated_reason` |
| `GET .../graph/lineage/timeline?room_id=&[step=\|at=]` | `as_of`, `lanes` (per node: `spans` = recorded state periods with `open`/`terminal`), `markers` (edge events; `kind` `edge`\|`edge_removed`\|`head_change`; `stale`), `range`, `truncated_reason` |
| `GET .../graph/lineage/events?room_id=&cursor=<opaque token>&limit=` | the reader's visible ledger page: allow-listed fields only (no raw `attrs`), `step` 1.. , `next_cursor` |
They exist only in `CollabService.graph_read` (never in the keyed `/api/v1/graph`, so a fleet key gets no unbudgeted replay), only in
the combined server (`collab_only` => 404), and a non-member, guest, agent or unknown room all answer the same 404. Bad parameters
are 400 (`step`/`at` together, negative step, non-ISO `at`, unknown type, limit out of range). Reads are rate limited per person (240/min, 429).

### Replay semantics
- **Position** is a count of the READER's own visible ledger events (`step`, 0..`visible_events`), so no number reveals how many
  hidden events exist. Past the end clamps to now (silently). `at` (a timestamp without a zone is UTC) resolves, by server receipt time (never the emitter's
  `occurred_at`), to the number of visible events BEFORE the first one received after that instant: a prefix of admission order.
- **Replay equals live at that point.** The graph at step k is a projection built from the reader's first k visible events
  of this room (the room and its linked missions), read through the live projection's gate (which supplies visibility).
  `apply` is commutative and idempotent, so the visible result equals a projection that was fed those events live (tested at
  every step, for several readers, after a rebuild, and by a randomized privacy check). Replay is per room: a flood in room A
  never costs or breaks room B. Steps are exact and self-consistent: every step from 0 to the head is replayable, the head's
  step is the real count of events in the window, and no rounding is ever exposed. When the window is trimmed (below), a replay
  shows the window's own events alone, so an object created before the window appears only once an in-window event names it.
- **Replay never widens visibility.** (1) An event is shown only if its own scope label is visible to the reader and the object it
  concerns is visible to the reader NOW: for an edge event that is the exact edge instance it touched (its binding, and its writer's
  instance for a commons `from` node), never a sibling binding of the same triple; for a commons node event it is the writer's
  CURRENT value of every field the event carried. (2) A replayed projection reads through the CURRENT projection: a node or
  edge in the past is visible only if it is also visible now (tombstones are history and ignored; a later narrowing is not), and a commons
  field value of the past is shown only if the same writer's current value of that field is visible, so a writer who later makes a
  field private (or re-scopes an edge or node narrower) takes its past out of sight too. `stale`/`bindings` of the past never reveal a
  binding that is hidden now. What stays visible is only what the reader can see now in the same place: a value a writer published to
  the room and has since REPLACED with another room-visible value is shown at its own past position (that is what a replay is).
  (3) Membership is the reader's membership at THIS request; someone removed gets 404 at every step. **An overwritten
  room-visible value stays readable by whoever may read the room, including people who join later, when
  `replay_history` is `full`.** What a late joiner may see of the past is operator policy, `control_plane.collab.replay_history`
  in the fleet YAML: `since_join` (the default, strict) starts a member's ledger and replay at their join time: events are
  filtered one by one by receipt time (no assumption that timestamps are monotonic), step 0 is an empty graph, and the head
  shows CURRENT values only, with the identity and time of any event from before the join (`first_event_id`, `last_event_id`,
  `event_id`, `admitted_at`) set to null; an overwritten pre-join value is never shown. `full` lets every member replay the
  whole retained room history. A member's join time is the `created_at` the server stamped on the `room_created` or
  `member_added` event, so members from before this feature get the join time of that event; if no join time can be read at
  all, `since_join` fails closed (history starts at the moment of the request). Re-adding a member (also a role change) starts
  a new join time. Member responses never carry the global `admission` counter of a field.
- **Budgets** (all report `truncated_reason`, never a count of hidden items): the ledger window is the newest
  `LEDGER_SCAN_BUDGET` = 20,000 events THE READER MAY SEE (the room's log is scanned newest first, in chunks, until that many
  visible events are found or it is exhausted; `window_trimmed` says it was cut), so the window and every count in it
  (`visible_events`, the head `step`) are in the reader's own units and cannot be used to infer hidden volume. Page tokens
  (`next_cursor`) are opaque, masked, bound to the reader and room and tamper-checked (HMAC), so paging reveals nothing
  either. A replay applies at most `REPLAY_MAX_EVENTS` = 25,000 of the READER'S visible events (a hidden event costs
  nothing; over it is a generic 400, "now" is never capped). Each room/mission context keeps at most
  `LOG_MAX_PER_CONTEXT` = 100,000 events (the oldest tenth is evicted beyond that: the history floor); after an eviction the
  window is flagged trimmed and replay covers what is retained. Visibility is evaluated 128 events per hold of the live lock,
  so a viewer never stalls ingest for a whole scan. A head response (graph, timeline, ledger) is computed against one state:
  if an event arrived while it was built it is retried, and the third try runs under the lock. Graph reads reuse
  `SCAN_BUDGET`, `EDGE_SCAN_TOTAL`, `MAX_EDGES`, `MAX_RESPONSE_BYTES`; the timeline caps lanes (300) and markers (1000). At most
  two replayed projections are cached (keyed by reader, position and the count of applied events); one replay is built at a time.
- **Pagination.** `limit` (<= 1000) and `offset` page the room's visible nodes (types in fixed order, admission order inside a
  type); a page also carries the edges between its nodes and the nodes of earlier pages. The `SCAN_BUDGET` of examined ids is shared
  round-robin between node types, so one busy kind cannot starve the others. The web page does not pass `node_types` (it filters
  client-side over what was loaded).

### Timeline
Spans come from recorded `node.upsert`/`node.state` states (a span ends at the next state; a terminal state `done failed cancelled
superseded merged closed passed completed success error rejected approved aborted` is an instant; the last non-terminal span is `open`
and drawn to the end of the observed range, never beyond). Markers come from edge events. `head_change` = a `reviewed`/`validated`
edge bound to a different revision than an earlier visible one for the same subject; `stale` is the projection's own flag for
that edge as the reader sees it at that position (T02 semantics: related edges only, admission order unless `attrs.supersedes`).
Times are server receipt times; the emitter's claim (`occurred_at`, which falls back to receipt time) travels alongside and the UI
offers it as a separate choice. Nothing is inferred from chat, layout or timing, and no progress is invented.

### UI and accessibility
Graph (layered SVG, no library), table view of the same data (always available; the only view above 300 nodes), Gantt (SVG +
table), replay (range slider with spoken `aria-valuetext`, step buttons, Live), filters (search, node type, link type, state),
inspector (read-only: fields, attributes as text, links in/out, state history, provenance). Keyboard: one tab stop in the graph,
arrows/Home/End between nodes, Enter selects, Escape clears; arrow keys switch Graph/Gantt tabs. Nothing is encoded by colour alone:
node types differ by shape, link types by dash pattern and a text tag (`stale` is struck through and says so), states by glyph and word,
Gantt states by fill pattern and label, head changes by a letter. No animation (so nothing to reduce); forced-colors supported.
Every id, label, attribute and reason is escaped for text and attribute contexts (unit and browser tested with hostile values).

### Not in this task / limitations
- Click-to-inspect terminal is NOT offered (needs `authorize_workspace`; the inspector says so). No comments, fork, steer or retry.
- T05 (canonical GitHub issue/PR linking and reconciliation) is not built: issue/PR nodes and `issue_link` edges are drawn when an
  emitter sends them, but no canonicalisation or reconciliation exists yet. T06's `review_quorum.py` is on a sibling branch and
  is not on this stack; stale/head-change come only from the T02 projection's own flags.
- Replay cost is the reader's visible prefix (see budgets); beyond 25,000 visible events a past position answers 400. A busy room needs
  the snapshot table mentioned above. Not load-tested at that size. No visual-regression suite exists; layout is checked by
  unit tests and a browser smoke run against a mocked API plus one real-HTTP pytest of the routes, not by screenshots.
- There is no separate review/CI phase model. PR, review and CI objects appear as lanes of their node types (state spans from
  `node.state`), `reviewed`/`validated` edges as markers, and revision changes as `head_change` markers; nothing else is inferred.
- The inspector shows the node as of the chosen position, from the page's own response; it does not fetch `graph/nodes/{id}`.
- Pi still does not send `room_id`, so Pi lineage appears here only once the T08 emitter prerequisites hold.
- Memory: each context list holds references to the dicts the projection already receives (bounded, see budgets); at most two
  replayed projections are cached, each of at most `REPLAY_MAX_EVENTS` events.

### T09 rollout and rollback
Code only, no migration: deploy the server, then the web files (`collab-lineage.js` is in the static map in `api.py`). Rollback:
revert the PR; stored events are untouched and the T08 page is unchanged (a web-only rollback removes the Lineage tab).

## Comments and steering (T12)
Discussion on graph nodes and edges, and explicit, two-step requests to change what a running attempt does. Both extend the
HCN service (`herdr_engineering/control_plane/collab_workbench.py`, mixed into `CollabService`) and the T09 inspector
(`packages/collab-workbench.js`, wired in `collab-view.js`). **No new service, event store or table, no migration:** every
change is an immutable collab event in the same durable log, applied deterministically (epochs are stored in the event; the
clock is never read inside `_apply`), so a restart rebuilds the same state. Lineage-owner delivery uses the existing keyed
API. Neither feature exists in `collab_only` mode (no graph there): the routes answer 404.

### Comments (discussion only)
- `POST /api/v1/collab/rooms/{room}/comments {anchor, text, idempotency_key}`; anchor is `{"kind":"node","node_id"}` or
  `{"kind":"edge","edge_type","from_id","to_id"}`. `GET .../comments[?anchor_kind=node&node_id=|edge_type=&from_id=&to_id=&limit=&before=]`,
  `POST .../comments/{id}/edit {text, expected_revision}`, `POST .../comments/{id}/retract {expected_revision}`,
  `GET .../comments/{id}/history`.
- **Scope.** Room-scoped. Writing needs a signed-in, non-guest, approved member with the `post` right; reading needs the `read`
  right and graph access (`CollabService.graph_access`, T08). Agents, guests, viewers-for-writing and non-members get 403/404.
  The anchor is checked with the graph principal NARROWED to that one room (`rooms`, `room_ws`, `room_missions` of this room only),
  so a node visible only through room B (or another mission) cannot be commented on from room A. Hidden and missing anchors are
  the same 404. **The check is repeated at read time for every comment**: if the anchor later narrows or the member loses access,
  the comment text disappears with it (it would otherwise be a side channel for the hidden node).
- **Immutable history.** An edit or retract appends a revision (`comment_revised`); nothing is overwritten. `expected_revision`
  prevents lost updates (409 `stale_revision`). Only the author edits; the author or a room owner retracts (an owner's
  removal is `moderated`: its history text is withheld from everyone but the owners and the author). Up to
  `max_comment_revisions` (10) revisions.
- **Bounds.** Text 1..`max_comment_chars` (2000), 30 new comments or revisions per person per 10 minutes (150 per room),
  200 comments per anchor, 5000 per room, 10 revisions per comment; idempotent by `(principal, room, key)`.
- **Untrusted.** Text is stored redacted, with C0/C1 controls, bidi overrides/isolates, zero-width characters and
  U+2028/2029 removed (they can paint a fake "system" line or reverse a name). Responses carry `kind: "comment"`, `trust: "untrusted"`
  and a server-set `author`. The page renders text only through `esc()`. **Comments never enter the room feed**
  (`_FEED_TYPES`), so `room_read`, MCP, A2A and guests cannot see them, and **no comment is ever delivered to an agent**. The audit
  trail records `comment.add/edit/retract` but not the text.

### Steering (explicit, typed, two-step)
Closed set of actions: `pause`, `resume`, `stop_after_step`, `redirect` (the only one with free text, `instruction` <= 1000 chars),
`approve`, `reject`. There is no command, shell, argv or terminal field anywhere; unknown fields are 400.
- **Targets.** Control actions: `attempt`, `agent`, `task` or `mission` nodes. `approve`/`reject`: `gate` nodes only, and only with
  `expected_digest` equal to a digest/commit the gate carries. **`plan` and `review` nodes are refused: plan decisions belong to
  Plannotator** and steering must not stand in for a Plannotator receipt. Commons nodes (`pr`, `commit`, ...) have no single
  owner and cannot be steered. A terminal-state target is refused.
- **Step 1, propose** `POST .../rooms/{room}/steering {action, node_id, expected_fence, [instruction|expected_digest], [reason], idempotency_key}`:
  a signed-in person with the room's `task` right (owner/contributor). `GET .../steering/target?node_id=` returns the current
  `fence`, eligible actions, the pinned source and whether you may propose/confirm. The proposal is a `steer_proposed` event.
- **Step 2, confirm** `POST .../steering/{id}/confirm {digest, expected_fence}`: a member whose role is in `confirm_roles`
  (default `owner`). The proposer cannot confirm their own request unless the operator sets `self_confirm: true` (a single-owner
  room therefore needs a contributor to propose, or that explicit opt-in). The digest binds the confirmation to the exact stored
  proposal. Confirmation is single use and idempotent (a replay returns the same record, no second event). `.../reject` withdraws
  (proposer) or declines (confirmer) and still works while steering is switched off.
- **Fence.** `fence = sha256(node id, type, state, last_event_id, superseded, owner, fencing_token/plan_digest/commit/head_sha/attempt_id)`.
  A target is **superseded** when a live visible `supersession` edge points at it (new -> old). The fence is checked at proposal
  (the client's `expected_fence`), at confirmation (and against the confirmer's own fresh read) and again at delivery. A mismatch
  closes the request as `fenced` (409 `stale_fence`); a superseded or finished attempt is 409 `stale_target`. Any change to the
  target node's state or attributes after the human saw it therefore cancels the request: propose again. Costs: a target that
  updates attributes often will fence often.
- **Delivery.** To the graph principal that CREATED the target node (the ingest key/actor that reported it): that is the "target
  agent" as far as Herdr can know. `GET /api/v1/steering/pending` (any valid API key; returns only requests pinned to the caller's
  key digest) and `POST /api/v1/steering/{id}/ack {outcome: applied|refused|unsupported|stale, note}` (ingest keys may make only this
  one extra POST). Not an HCN agent credential: agents never receive or send steering through MCP/A2A. Each item carries
  `steering_id` (the idempotency key: apply once), `trust: "untrusted"`, `provenance`, `proposed_by`, `confirmed_by`, the room, the
  target and fence, `valid_until_epoch`, and a fixed notice telling the receiver the free text is untrusted instruction text from a
  named person, never authority to run commands, and to answer `unsupported` rather than fake an action. At fetch time Herdr re-checks
  that steering is still on, the proposer still holds `task`, the confirmer still holds a confirming role, the room still exists and
  the fence still matches; otherwise the request is closed (`rejected`/`fenced`) and not delivered. A request is re-offered until
  acked at most 5 times and for `delivery_ttl_seconds` (300); an unconfirmed proposal lives `proposal_ttl_seconds` (900).
- **Replay.** Terminal states (`acked`, `rejected`, `withdrawn`, `fenced`) are never delivered again; a confirmed id is never
  re-confirmed; an ack with a different outcome after the first is 409; re-adding a removed proposer does not revive anything.
- **Flood.** 10 proposals per person per 10 minutes (50 per room), at most 3 open per target and 50 per room, 30 confirmations per
  10 minutes, 2000 requests of history per room, 600 polls and 600 acks per key per minute.
- **Audit.** `steer.propose/confirm/deliver/ack/rejected/withdrawn/fenced` appear in the project audit with actor and outcome,
  without free text.

### Operator YAML (no flags, no environment variables)
```yaml
control_plane:
  collab:
    steering:
      enabled: false          # default: OFF. Comments follow this switch (and kill_switch) unless comments_enabled is set.
      kill_switch: false      # true stops proposal, confirmation and delivery at once
      actions: [pause, resume, stop_after_step, redirect, approve, reject]
      confirm_roles: [owner]  # owner and/or contributor
      self_confirm: false     # true lets a proposer confirm their own request
      proposal_ttl_seconds: 900
      delivery_ttl_seconds: 300
```
Other keys: `max_instruction_chars`, `propose_per_10min`, `confirm_per_10min`, `max_open_per_target`, `max_open_per_room`,
`comments_enabled` (unset: comments follow `enabled` and `kill_switch`; an explicit true or false decides alone; retract and moderate always work), `comments_per_10min`, `max_comment_chars`, `max_comment_revisions`. Unknown keys or wrong types stop startup
(and the rendered collab slice). When a fleet config file is in use the service re-reads this section on every request, so editing
`kill_switch: true` takes effect without a restart; an unreadable or invalid section fails CLOSED (steering and comments off).

### Threat model (what was considered)
| threat | answer |
|---|---|
| cross-room comment injection / reading | anchor checked with a one-room principal at write and at every read; non-members and other rooms get one 404 |
| guest or agent steering / commenting | user principals only; agents 403, guests/non-members 404; viewers cannot write |
| unauthenticated steering | HCN routes need a verified token; the delivery routes need an API key and return only requests pinned to that key |
| one person proposing and confirming | proposer != confirmer unless the operator opts in; confirm needs the owner role |
| replay of a confirmed request | single-use confirm, terminal states never delivered, `steering_id` is the receiver's idempotency key |
| steering after revocation | membership/role/approval re-checked at fetch; re-adding a person revives nothing; kill switch re-read per request |
| stale head / superseded attempt | three fence checks; `supersession` edges and terminal states refuse |
| comment text spoofing a system message | server-set `author`/`kind`, spoofing characters stripped, never in the feed, never delivered |
| shell / terminal injection | closed action set, one bounded untrusted text field, no route to a terminal; Pi maps actions to its own typed primitives |
| flood / log exhaustion | per-person, per-room, per-anchor, per-target caps; the shared 200k log cap is still the hard limit |
| squatting a target id | known T02 limitation: delivery goes to whoever created the node first; the confirmer sees `pinned_source` |

### Limitations
- Delivery is **pull**, to the source that reported the node. Pi must poll; Herdr does not push into a host.
- "The target agent" is the node's creating ingest principal, not an HCN agent identity. Two agents sharing one ingest key
  share steering. An id squatter who created `attempt:x` first receives steering for it (T02 limitation, shown as `pinned_source`).
- The fence includes `last_event_id`, so any state/attribute update fences pending requests.
- Approve/reject are generic gate requests; the receiving system decides what a gate is. They never replace Plannotator.
- Only the combined server has a graph: the dashboard proxy to an isolated `collab_only` process answers 404 for comments and steering.
- No visual-regression suite exists; the UI is covered by unit tests and a Playwright run against a mocked API.
- The UI for links (edges) offers comments only (steering targets are nodes).

### Review follow-ups (T12)
- Retract and moderate are exempt from `max_comment_revisions` and from the comment rate limit; only edits count.
- Only OPEN steering requests count toward the per-room cap; closed ones are history. Retracted comments do not count toward the room or item comment caps (an owner can always free room by removing comments).
- `POST /api/v1/steering/<id>/ack` answers 410 (`expired`) once the delivery window has closed.
- Every offer, redeliveries included, is a `steer_delivered` audit event with its `delivery` number; the counter is rebuilt from those events on restart.
- `GET /api/v1/steering/pending?instance_id=<id>`: when the target node carries an `instance_id` attribute, only a caller presenting that id is offered the request, so two Pi instances sharing one key do not consume each other's requests. Nodes without the attribute keep key-only pinning.
- The steering routes refuse (403, `authentication_required`) on an open listener, which has no principal to pin to.

## Forks and promotion (T13)

Alternative implementations reuse the existing vocabulary; there is no second store.

- A fork is an OWNED node `approach:<id>` (written by the Pi emitter) plus a `fork` edge from the origin
  (`attempt`, `agent`, `task` or another `approach`) to it. Siblings are approaches that share an origin.
- A decision is a `supersession` edge winner -> loser. Nothing is ever deleted; the loser is only marked
  `superseded`, the winner `selected`.
- Approach attrs (all self-reported by the fork's own emitter, shown as `source: approach_owner, verified: false`
  and never used to gate anything): `approach_id, origin_id, mission_id, task_id, base_sha, head_sha, branch, repo,
  worktree_id, files_changed, insertions, deletions, touched_paths, elapsed_s, cost_usd, tokens, pr, pushed_head`.
- Fork and promote are steering actions (T12 flow: propose, a different member confirms, keyed delivery to the
  node owner's key, ack). They are not in the default `actions`; both the `actions` list and
  `control_plane.collab.steering.forks.enabled: true` must opt in. YAML under `steering.forks`:
  `enabled`, `max_forks_per_origin` (5), `max_open_forks_per_room` (20), `require_validation` (true),
  `min_reviews` (0), `evidence_writers` (principals trusted to write validation/review evidence), `max_compared` (10).
- `fork` needs a `base_sha` that the graph shows for the origin. `promote` needs a live, non-superseded approach
  with a full `head_sha` and `expected_head`; readiness is re-checked at propose, confirm and delivery.
- Evidence that counts: `validated` / `reviewed` (verdict `lgtm`) edges bound to the exact head, not stale or removed,
  written by an `evidence_writers` principal. A `blocked` edge from such a writer blocks promotion.
- Promotion race: the earliest confirmation wins, an acked-applied promotion closes the group, and Pi holds a
  cross-process claim per origin. Pi pushes exactly the confirmed sha (never forced), opens a DRAFT PR, links it
  (T05) and acks `applied` only after all three. It never merges.
- `GET /api/v1/collab/rooms/{id}/forks?node_id=` returns the comparison read model (403 `forks_disabled` when off).

| Threat | Control | Tests (`tests/control_plane/test_forks.py` unless noted) |
|---|---|---|
| Fork flood, disk | per-origin and per-room caps on both sides, free-disk floor on Pi | `test_fork_flood_is_capped_per_origin_and_per_room`, `test_a_fork_is_refused_at_confirmation_when_the_limit_filled_meanwhile`; Pi `flood and disk limits` |
| Fork of a node the actor cannot see | target resolved through room visibility | `test_who_may_fork_and_what_they_cannot_see`, `test_comparison_hides_what_the_reader_cannot_see_and_is_bounded` |
| Stale, superseded or unvalidated promotion | `promotion_check` at propose, confirm, delivery; Pi re-checks the head | `test_promotion_is_refused_until_trusted_head_bound_validation_exists`, `test_promote_needs_the_exact_head_the_person_saw`, `test_only_approaches_can_be_promoted_and_a_losing_fork_cannot`, `test_stale_head_after_confirmation_never_reaches_the_pusher`, `test_revoked_evidence_between_confirmation_and_delivery_blocks_delivery`, `test_trusted_evidence_removed_after_proposal_blocks_confirmation` |
| Comparison forged by the fork's agent | self-reported labels; only operator-listed writers count | `test_comparison_shows_every_sibling_with_provenance_and_never_trusts_the_forks_own_numbers`, `test_the_forks_own_agent_cannot_forge_evidence`, `test_no_trusted_writers_configured_means_nothing_can_ever_validate` |
| Promotion race | `_promotion_conflict` (earliest confirmation wins) plus Pi registry claim | `test_promotion_race_only_one_sibling_can_be_confirmed_and_delivered`, `test_an_applied_promotion_closes_the_group_even_if_the_state_is_not_reported_yet`, `test_a_refused_or_stale_promotion_does_not_block_the_next_one`; Pi `only one promotion per origin`, `two processes sharing a state dir` (two registry instances in one process, not separate processes) |
| Branch or path injection | Pi derives every name from the steering id; params are shas and a cleaned label | `test_nothing_in_a_fork_or_promote_delivery_is_a_command`, `test_hostile_text_in_a_fork_is_neutralised`; Pi `derives every name itself`, `fork parameters are typed and strict`, wire-fixture test |
| Closing siblings destroys work | siblings only marked; branches and worktrees kept; no delete in `CliForkGit` | Pi `pushes exactly the confirmed commit, opens a DRAFT PR...`, `refuses rather than delete` |
| Confused deputy across rooms | room checked on propose, delivery and on Pi | `test_http_routes`, `test_self_confirmation_and_replay_are_refused_like_any_steering`; Pi `never promotes from another room` |

Unmet or partial against the spec, stated plainly: a fork prepares an isolated worktree and records it but does not
start an agent in it; "invalidates stale reviews" is done by binding evidence to the exact head (a moved head makes
evidence stale at read time and blocks promotion) and no gate node is rewritten; promotion is unreachable until the
operator lists `evidence_writers` and something writes `validated` edges for the fork head.

Limitations: T06 review quorum is not on this stack (comparison reports `quorum: unavailable`); independent
evidence writers must be keys that can see room-scoped nodes, i.e. operator keys; an older Pi ignores the new actions
and Herdr re-offers up to 5 times.
