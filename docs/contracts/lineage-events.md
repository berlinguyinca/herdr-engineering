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

### Head-bound review quorum (T06)
Two different meanings of "stale" exist and must not be confused:
- the graph edge flag `stale` (above) = "a later binding was admitted by a related edge". It is arrival-order based and
  cannot know which commit is current;
- the quorum's `stale_reviewers` = "bound to a commit other than the PR's CURRENT head". It is head-relative.
After a force-push back to an earlier SHA the edge flag may say `stale: true` while the quorum counts that review again,
because the SHA names the content. Gate on the quorum, show the flag only as a hint.

`GET /api/v1/graph/nodes/{pr_id}/review-quorum` (viewer level; 404 when the PR is absent/hidden or the feature is off)
DERIVES the quorum; it writes nothing and Herdr does not request reviews itself. `rereview_required` / `rereview_needed`
are the re-review request: whoever owns review dispatch (Pi on `github.evidence_stale`, AutoSpec) polls or subscribes.

Producer contract. Each independent reviewer publishes with its OWN ingest key (reviewer identity is server-authenticated;
a shared emitter key is one identity): `node.upsert review:<id>` then `edge.add reviewed review:<id> -> pr:<id>` with
`attrs.commit` (full head SHA, the binding), `attrs.verdict` (`lgtm`|`blocked`, case-insensitive), `attrs.receipt_digest`
(64 hex) and `attrs.receipt_binding` = `sha256("pr-review-receipt-v1\0" lower(pr node id) "\0" commit "\0" reviewer principal
id "\0" receipt_digest)`. `autospec review-quorum publish` does this. Consumer: `autospec review-quorum request` polls this
route and runs the configured review dispatch once per (PR, head) when `rereview_required` is true.

Configuration (fleet config, `control_plane.review_quorum`, never from events):
```yaml
review_quorum:
  required: 2                      # approvals needed at the current head, 1..16
  trusted_head_writers: [key:<pi>] # principal ids whose head_sha / author writes define the PR's head
  reviewers:                       # principal ids that may vote; login optional (recognises the PR author)
    key:<reviewer-a>: {}
    key:<author-human>: {login: some-login}
  reviewer_groups:                 # optional: ids of ONE person/organisation count once
    - [key:<alice-laptop>, key:<alice-ci>]
```
ONE policy, two implementations: this route (workbench) and `autospec review-quorum` (merge gating) are pinned by the shared
vector file `tests/control_plane/fixtures/review-quorum-vectors.json` (byte-identical to autospec's
`crates/autospec-core/tests/fixtures/`); both suites load it. Rules:
1. The head is the newest non-empty `attrs.head_sha` written by a trusted writer and must be a full SHA, else `unknown_head`.
   `pr` is a commons node, so untrusted writers' values are ignored. A write that arrives AFTER a higher-sequence one from
   the same writer is either a late delivery or an emitter restart that reset `source_sequence`:
   - same head value, or within 32 sequence numbers of the applied write: a late/duplicate delivery. Ignored; the applied
     head stands and nothing is poisoned (benign reordering never locks the PR).
   - a different head far below (more than 32): suspected restart. Status `head_unverified` (response also carries
     `head_unverified: true`, a human-readable `alert`, and for operators `resync_event_id`; one WARNING is logged per
     observation; `autospec review-quorum request` exits 4 on it instead of reporting "not needed"). Never satisfied. It
     clears when: (a) the writer writes past the applied sequence; (b) a second, increasing-sequence write follows (a restart
     run is recognised, the newest value is adopted, `head_rebased: true`); or (c) an operator acknowledges the observed write
     by writing `attrs.head_resync = "<resync_event_id>"` on the PR node (honoured only from an operator principal and only for
     that exact write). No emitter epoch exists yet; with one (Pi), the 32-number heuristic could be replaced by an exact test.
2. Only configured reviewers are read; anyone else's edges (approvals or blocks) are ignored, so junk cannot satisfy or block.
3. A reviewer is the edge OWNER (server-authenticated), never a payload name. `verdict`/`receipt_*` and add/remove are read ONLY
   from the owner's own writes: an operator or `co_writers` member cannot flip a verdict or retract a block.
4. Blocks first, never dropped: a configured reviewer's `blocked` at the head, at a head prefix or at an unparseable commit
   blocks (fail closed); at another full head it is stale. Many review nodes of one identity count once.
5. An approval is excluded if the reviewer owns a `produced` edge into the PR, or their configured `login` equals the `author`
   attr written by a trusted writer (`x[bot]` equals `x`). A missing/blank author is `unknown_author` (never satisfied; a blank
   write does not clear a known author, it just carries no value).
6. An approval needs a 64-hex `receipt_digest` and a `receipt_binding` that recomputes; one digest backing two groups voids both.
7. Verdicts for other heads are `stale_reviewers`, never counted; a verdict that lands after the head moved is stale; one issued
   for a head not yet observed waits until a trusted writer reports that head.
8. Matching is ASCII-only, identical in autospec: whitespace is the six ASCII blanks, case folding touches only A-Z, ids and
   targets must be printable ASCII (`invalid_target` otherwise); no Unicode folding (Kelvin sign, dotted I, NBSP).
   `rereview_needed` is computed from the approvals the CALLER can see only, so a hidden approval never shifts it.
9. A mistaken verdict is retracted by its owner at the same head: `autospec review-quorum publish --retract` sends an
   `edge.remove` (same key, same edge) which Herdr honours only from the edge's owner.
10. The decision is computed over ALL matching edges. A caller sees names only of reviewers whose edges it can see; it also gets
   `has_hidden_reviews` and `blocked_by_hidden` (a hidden block still yields `blocked`; existence is disclosed, counts are not).
Statuses: `satisfied`, `insufficient` (re-review needed), `blocked`, `unknown_head`, `head_unverified`, `unknown_author`, `invalid_target`. Only
`satisfied` passes. PR ids are matched exactly, then lower-cased.
Residual: the binding is not a signature (a colluding key can recompute it); two unlisted keys of one person count twice;
a verdict is bound to the head SHA, not the base; private/mission-scoped edges still count; Herdr ignores blocks from
unconfigured principals (anti-DoS) while autospec, with no `eligible_reviewers`, honours a block from anyone.

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
- **Visibility has no room/mission membership resolution yet**: `private` = the writer only, `mission`/`room` = operators only,
  `org` = any authenticated key. T08/T09 will wire room membership.
