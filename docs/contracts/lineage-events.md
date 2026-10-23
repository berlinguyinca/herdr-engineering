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
