# ADR-0008 — Independent Herdr-server federation

**Status:** Accepted 2026-10-25 — owner accepted the recommended answers to D1–D10 (see "Owner decisions: resolved"). Implementation proceeds phase by phase (P1 first), each its own reviewed PR; P4 needs a separate approval.

Program task: T17 of `herdr-federated-engineering-20261022` (`docs/specs/herdr-federated-engineering-20261022/SPEC.md`,
"Server-to-server federation separate later gated feature, not implied by external agent participation").
Grounded in the merged control plane at `a838757` (main, after T16). Code is cited as `file:Symbol`, never by line.

This ADR changes no code, no configuration and no behaviour. Every "must" below is a requirement on a future implementation PR (P1 onward), one phase per PR.

## Context

Today one Herdr control plane is the only authority for its organisation. Two things look like federation but are not:

* **External agents** (T10, `docs/operations/external-agents.md`) are one coworker's agent, enrolled into ONE room of THIS server with an
  expiring credential. The server is still single-tenant; the agent is a guest principal.
* **Several hosts** report into one plane (`docs/operations/acceptance-evidence.md`: "two ingest keys, sources, host ids"). Still one server.

What does not exist: two independently operated Herdr servers (two organisations, or two sites of one organisation that must keep running
when the link is down) sharing selected rooms, graph nodes or review evidence, each keeping its own users, keys and policy.
The use case is cross-organisation engineering collaboration (a partner lab's room that shares a task and its evidence with ours) and
site autonomy (a campus plane and a cluster plane that keep working apart). It must not weaken the standalone-Pi guarantee: Pi, and a single
Herdr, continue to work with federation absent.

### Constraints found in the merged code

| # | Fact (source) | Consequence for federation |
|---|---|---|
| C1 | `collab.py:ORG_ID = "default"`, commented "single-organization deployment (spec: no federation in v1)". The HCN spec says "design contracts for eventual federation". | `org` scope means "everyone on this server". Export cannot be derived from `visibility_scope`. |
| C2 | `envelope.py:normalize_envelope`: `visibility_scope` is only validated against `private|mission|room|org` (unknown becomes `private`); `docs/contracts/event-envelope-v2.md`: "No per-principal visibility grant exists yet, so the server only validates the enum; it does not narrow further". | Inbound peer events cannot be clamped today. A clamp is a hard prerequisite (P1). |
| C3 | Actor identity is `key:<12-hex sha256 of API key>` with `kind` = the key's configured role (`envelope.py:_actor_id`, `api.py` `lineage_key_scopes`). `ingest.py:IngestService._check_lineage_scope` skips the room/mission check for an authenticated `operator` actor, and `"*"` in an allow-list opts a key into every id. | A peer must never be `operator`, and `"*"` must be rejected for peer principals. |
| C4 | `envelope.py:SourceLedger`: sequence domain `(actor.id, source)`, gap tracking, freshness (`stale` never `healthy` by default), `event_id` unique per actor (migration 0005), `MAX_SOURCES_PER_ACTOR = 32`, `MAX_SOURCES = 1024`. | A natural replication substrate: at-least-once, idempotent, out-of-order tolerant, one peer cannot evict another's state. |
| C5 | `collab.py:CollabService.hydrate` requires the `collab` log to be one contiguous `sequence` and raises on a gap "because replaying it could drop a revocation". `record_required` is the durable write. Log caps pre-pay revocations (`SAFETY_EVENTS`, `REVOCABLE_CREATE`). | Remote events cannot be spliced into the local log. Anything peer-related that creates authority must be a revocable creation with a pre-paid revocation. |
| C6 | `ingest.py:IngestService._check_reserved_nodes` and `reserved_node` / `service_node` hooks: external-agent namespaces and `task:hcn-delegation.*` / `artifact:hcn-proposal.*` are writable only by their owner, the HCN service (`collab` principal kind) or an authenticated operator. | Replicated writes must be refused in those namespaces; a peer is none of the permitted writers. |
| C7 | `review_quorum.py:QuorumConfig`: reviewers, `trusted_head_writers`, `reviewer_groups` come only from operator YAML, never from events; the policy vectors `tests/fixtures/review-quorum-vectors.json` are byte-identical with AutoSpec's `autonomous::review_quorum`. | A remote review counts only if the local operator lists the remote principal; changing the rule is a two-repository change. |
| C8 | `collab_workbench.py` steering: a request is bound to the target's fence (`_FENCE_ATTRS`: `plan_digest`, `digest`, `commit`, `head_sha`, `attempt_id`, `fencing_token`), proposed and confirmed by two different local people, and delivered only to the graph principal that created the target node. | The fence is local-state truth. A remote server cannot evaluate it, and a remote person is not a local room owner. |
| C9 | `collab_delegation.py` budgets echo `max_hops` "so a future onward path has the bound already"; an external agent has no route to delegate onward. | The hop and cycle bound for a federated hop already has a field and a unit. |
| C10 | ADR-0007 #1: "A separate gateway would duplicate the sink, the limiter and deployment; revisit for federation." #4: agents connect outbound; there are no inbound ports for peers. `collab_a2a.py` is a stateless text-only adapter with no task store. | The transport and process-model decisions are explicitly left open here. |
| C11 | `collab_context.py` cross-links (`xlink_*`) connect two workspaces visible to one room, valid only while both grants and the creator's membership hold. | Cross-links stay intra-server; a peer link is a different object (see Decision 7). |
| C12 | Control-plane configuration is YAML only, hot-reread, fail-closed (`delegation_enabled`, `steering.kill_switch`); no environment variable. | Federation is gated the same way: default off, YAML only, one kill switch. |

## Options

1. **No federation (status quo).** Partners use external agents inside one room. Cheapest and safest; cannot give a partner a server of their
   own, offers no site autonomy, and makes the host organisation the single authority and single point of trust.
2. **Shared database or shared log between servers.** Rejected. It breaks C5 (contiguity), merges two trust domains, and makes revocation depend on
   the other side's availability.
3. **One central hub server that everyone joins.** This is option 1 with a bigger room. It re-creates a single authority and a cross-org
   tenant boundary that C1 says does not exist.
4. **Bilateral, pull-based mirror of explicitly shared rooms (recommended).** Each server stays authoritative for what it created. A peer pulls
   an allow-listed, redacted, append-only projection of shared rooms' lineage and message events over an outbound-initiated, mutually
   authenticated channel, and stores it as read-only mirrored data attributed to the peer. Writes to the other side's objects go only through
   typed, owner-evaluated requests (Decision 8), never as raw replicated state.
5. **Full multi-master replication (CRDT or consensus).** Rejected for now: no merge semantics for membership, grants, fences or review
   quorum, and every conflict is a security decision.

## Proposed decisions (all subject to approval)

1. **Option 4: bilateral peering, mirror not merge.** A federation link joins exactly two servers. No transitive trust: A peering with B and B
   with C gives A nothing from C. A hub topology is deferred (Owner decision D1).
2. **A peer is its own principal kind, `peer`, with its own namespace.** Never `operator`, never `collab`, never `external`. Peer principal
   ids carry the peer id and a key fingerprint. They are minted only from YAML under `control_plane.federation.peers.*` (default off,
   re-read per request, fail closed, no environment variable; C12). `_check_lineage_scope` and `_check_reserved_nodes` are extended so that
   `peer` is checked like a non-operator key and may never hold `"*"`; the YAML loader rejects `"*"` for a peer (C3, C6).
3. **Export is an explicit per-room allowlist, never a scope.** A room is exported to a named peer only if (a) the local operator YAML lists
   `{peer, room}` and (b) a current owner of that room has accepted the share through an HCN event (`federation_share_granted`). `org` and
   `private` scoped data never leave (C1). `mission`-scoped data leaves only with its linked room. A share is a revocable creation with a
   pre-paid revocation in `SAFETY_EVENTS` (C5). Revoking is immediate for new pulls.
4. **Inbound data is clamped server-side before it is stored.** The ingest path gains a peer clamp: every inbound event is rewritten to
   `visibility_scope: room` (never wider), bound to the single local mirror room created for that share, `actor` set to the `peer` principal,
   the original `{origin_server, origin_actor, origin_event_id, origin_sequence}` kept only as `claimed_*` provenance (the same rule the
   envelope already applies to `claimed_actor`). This clamp does not exist today (C2) and is **prerequisite P1**; no network code ships first.
5. **Replication reuses `SourceLedger`.** The sequence domain is `(peer principal id, "fed-" + share id)`, giving idempotency, gaps,
   out-of-order tolerance and freshness for free (C4). A peer that stops advancing is shown `stale`, never `healthy`. Mirrored events go to
   `fabric_events` as envelope v2 rows (a new `ENVELOPE_ONLY`-style entity handling for the mirror, to keep them out of the global SSE feed and
   `/activity`, as `collab` events are today). They never enter the local `collab` log, so contiguity and `hydrate` are untouched (C5).
   Only the small control events (`federation_peer_enabled`, `federation_share_granted`, `federation_share_revoked`) are `collab` events.
6. **Transport: outbound-initiated, authenticated both ways, pull by cursor.** Each server runs a puller that dials the peer's
   `/api/v1/federation/*` read endpoints over TLS, so a site behind NAT only needs outbound connectivity (consistent with ADR-0007 #4, C10).
   Peer identity is proven by a pinned public key that signs requests and exported envelopes (Owner decision D3 chooses the mechanism).
   No shared bearer secret is the only factor. Exports are signed so a relay or cache cannot alter them. A hosted gateway process is not
   introduced; the endpoints live in the existing server (ADR-0007 #1 revisited and kept).
7. **Mirrored objects are read-only, labelled and never executable.** Mirrored nodes carry `trust: "peer-asserted"`, the origin server name, and
   a node-id prefix `peer:<peer>.` that is reserved like the HCN prefixes (C6): no local key, agent or external can write it, and the mirror
   cannot write outside it. Local cross-links (C11) may point at a mirrored node only as a new, local, user-asserted object. Remote message
   text is untrusted data exactly as external-agent text is: stored and shown, never routed to a terminal, `herdr agent prompt`, or any agent as
   instructions (spec: "External messages untrusted and never executable").
8. **Writing across the boundary is a typed request, evaluated by the owner.** The owning server is the only place a fence, a grant or a
   room role is evaluated (C8). The only cross-boundary writes in scope for a future phase are: (a) a chat message into a shared room, and
   (b) an artifact proposal that lands in the local quarantine exactly as T11 does (`proposals`, digest-bound accept by a local room
   owner). Both consume the existing delegation budget including `max_hops`, whose federated hop counts as one and whose chain carries the
   list of server ids so a cycle is detected and refused (C9). Defaults: TTL, budget and `max_turns` are never wider than the local task's.
9. **Effective permission stays an intersection, now with two more terms.** A peer principal's authority is
   `local operator grant ∩ share (room owner consent) ∩ room role ceiling for peers (read, post only) ∩ expiry`, evaluated per request
   (the `_effective` pattern, default deny, 404 for the unshared). Room access never implies terminal access (spec): a peer never gets a
   terminal, pane or session link.
10. **Remote humans and agents are shadow principals.** A remote person who posts into a shared room appears as `kind: "peer-user"` with the
    origin server and display name attributed from the signed envelope, never as a local member, never counted as a local room owner, never able
    to confirm steering, decide a proposal or join the local roster.
11. **Reviews and steering do not cross in this ADR.** A review quorum (C7) counts only principals the local operator lists. A remote reviewer
    may be listed only by an explicit, separate decision (D5); until then mirrored `reviewed` edges are visible evidence and never satisfy
    local quorum. Steering is not federated (C8): a mirrored target has no local fence. Remote pause/cancel/fork is a non-goal.
12. **No cross-organisation leakage by construction.** Each server's secrets, key digests, member rosters, grants, egress allowlists, billing,
    usage, host inventory, terminal bytes, hidden reasoning and redacted fields are not in the export schema at all. The export is a
    fixed allowlist of fields per event type (an allowlist, not a denylist), re-run through the existing `redact.py` on the exporter. The
    importer re-validates every mirrored event with the same validators as local ingest and refuses oversize, unknown-type, reserved-prefix
    and secret-like content (it refuses, it does not repair, as T11 does).
13. **Gated like every other optional capability.** One YAML kill switch `control_plane.federation.enabled` (default false, hot-reread,
    unreadable or non-boolean means false), plus per-peer `enabled`. Off means: puller stops, export endpoints answer 404, mirrored data is
    retained but hidden and not readable until re-enabled or purged (D6). No environment variable, no flag.

## Data flow and consistency

```
Server A (authoritative for A's rooms)                 Server B
  room R (shared with B)  --signed export (pull)-->   puller (outbound from B)
  lineage + messages: field allowlist + redaction     clamp -> room-scope mirror of R under peer:A.
                                                       SourceLedger domain (A-peer, fed-<share>)
  <-- typed request (message / proposal), signed ---  B's user posts in the mirror room
  owner evaluates grant, budget, hops, cycle          result returns on the next pull
```

* **Authority is per origin.** Every object has exactly one owning server; a mirror never becomes authoritative. There are no conflicts to
  merge because there are no concurrent writers of the same object.
* **Delivery is at-least-once, idempotent, resumable.** Cursor = last contiguous source sequence per share (C4). A restart resumes from the
  durable ledger; a replay is `duplicate: true`. Order across shares is not guaranteed; within a share it is the origin's emission order.
* **Freshness is explicit.** The UI and API show last-successful-pull time and `stale` (C4), and never present mirrored state as live.
  Nothing is synthesised on a gap: a missing range is displayed as a gap, never filled (spec: "never fabricate progress").
* **Revocation wins.** After `federation_share_revoked` the exporter returns no new events for that share at the next request; the importer
  stops, and the mirror is hidden. Data already mirrored on the peer is outside the exporter's control: this is a stated limit, not a bug
  (D6 decides the deletion obligation).
* **Eventual, bounded.** A federated read is eventually consistent with a bound equal to the pull interval plus link latency; no operation in
  this ADR requires strong consistency across servers.

## Failure modes

| Failure | Behaviour |
|---|---|
| Link down or peer offline | Each side keeps full local function. Mirrors go `stale`. Typed requests queue in a bounded outbox with expiry, then fail visibly (never silently dropped). |
| Peer key compromised | Operator removes the peer in YAML (hot-read). Requests from that key are refused on the next request; shares are revoked; outstanding typed requests are voided. Other peers are unaffected (per-peer principal, C4). |
| Clock skew | `received_at` is always the receiver's clock (C2). Signatures carry a bounded validity window to limit replay. Ordering never depends on wall clock. |
| Replay or reorder | Idempotent by `(actor, event_id)` and `(actor, source, sequence)` (C4); conflicting content for a known id is a 409 and the original is preserved. |
| Sequence gap | Recorded as a gap, event accepted (C4). A gap that does not close within a window raises an attention item; nothing is invented. |
| Malicious or buggy peer floods | Per-peer rate limit and byte quota, at most 32 domains per peer, bounded mirror size per share; over quota the share is paused, not the server. |
| Peer sends an event in a reserved prefix, `org`/`private` scope, or an operator-looking actor | Refused before persistence (C3, C6); recorded as an `audit_denied`-class event. |
| Delegation loop A->B->A | Refused by chain list and `max_hops` (C9). |
| Partial import failure | The envelope row is persisted first, then the handler, as for local ingest; a retry is acknowledged as duplicate without re-applying. |
| Log capacity | Share grants are pre-paid revocable creations (C5); a full log cannot make a share irrevocable. |
| Mixed versions | An older peer sending an envelope above the receiver's `SCHEMA_VERSION` is rejected by `normalize_envelope` (fail closed); see D8. |

## Migration and rollout

* **Phase 0 (this ADR).** Docs only. No schema, no config key, no endpoint.
* **P1, prerequisite, separate PR, valuable alone.** Per-principal visibility clamp in `normalize_envelope`/`IngestService` (C2), and
  `peer` principal kind with reserved-prefix and `"*"` rules (C3, C6). Ships dark; no federation code.
* **P2.** Peer registry from YAML, share events, export endpoints, signed exports. Off by default.
  *Implemented (contract: `docs/contracts/federation-export.md`).* Notes against the text above: the operator side of Decision 3's
  `{peer, room}` list is `peers.<id>.export_rooms`; the pre-paid revocation of `federation_peer_enabled` is the additional event
  `federation_peer_disabled`; the export carries `message` events only (lineage nodes and edges need their own allowlist row and a
  reviewed reader, a later step); the P1 12-character fingerprint comparison is replaced by the full 64-hex key; request
  authentication is Ed25519 from `cryptography` (already in the `collab`/`review-gate` extras).
* **P3.** Importer/puller, mirror room, freshness UI. Read-only.
* **P4 (optional, separate approval).** Typed requests: message and proposal (Decision 8).
* Each phase is its own PR with tests and commit-bound evidence, flagged off, rollback by revert plus kill switch. New collab event types
  are additive and ignored by older code (the T10/T11 pattern). A new migration, if any, follows the 0004/0005 rule: deploy code first,
  apply the migration second, with a documented down-step. Standalone Pi and single-server Herdr are unchanged at every phase.

## Explicit non-goals

* Multi-master replication, shared database, shared log, consensus or CRDT merge.
* Transitive trust, hubs, mesh discovery or any automatic peer discovery.
* Federated authentication or single sign-on (each server keeps its own `UserAuthenticator`, e.g. Cognito, ADR-0007 #2).
* Federated steering, pause, cancel, fork, promote or any control action on another server's targets (C8).
* Remote terminal, pane, shell, session link or file-system access, ever.
* Remote reviews counting toward local quorum, until a separate decision (D5).
* Federating `org`/`private` data, member rosters, grants, egress rules, usage, host inventory or secrets.
* A second task store, second graph, second orchestrator or second credential system; Pi stays the execution authority.
* Making `ORG_ID` multi-tenant inside one server. This ADR federates servers, not tenants.
* Any implementation, test, config key or endpoint in this PR.

## Reuse audit (REUSE / EXTEND / NEW)

| Capability | Verdict | Source |
|---|---|---|
| Idempotency, sequence domains, gaps, freshness | REUSE | `control_plane/envelope.py:SourceLedger`, `normalize_envelope`; migrations 0004/0005 |
| Durable store of mirrored events | REUSE | `control_plane/sink.py`, `events.py` (`fabric_events`) |
| Scope and reserved-namespace enforcement | EXTEND | `control_plane/ingest.py:_check_lineage_scope`, `_check_reserved_nodes` (add `peer`) |
| Visibility clamp per principal | NEW (P1) | `envelope.py`, contract `docs/contracts/event-envelope-v2.md` |
| Share consent, revocation, pre-paid log cap | EXTEND | `control_plane/collab.py` (`SAFETY_EVENTS`, `REVOCABLE_CREATE`, `_effective`) |
| Budget, TTL, hops, cycle bound | EXTEND | `control_plane/collab_delegation.py` (`max_hops`, `max_turns`) |
| Proposal quarantine and digest-bound accept | REUSE | `collab_delegation.py`, `docs/operations/bounded-delegation.md` |
| Redaction | REUSE | `control_plane/redact.py` |
| Review quorum | REUSE (unchanged) | `control_plane/review_quorum.py`, `tests/fixtures/review-quorum-vectors.json` |
| Steering fence/digest | REUSE (local only) | `control_plane/collab_workbench.py` (`_FENCE_ATTRS`) |
| Peer registry, signed export, puller, mirror room | NEW | none exists |
| Implementation and tests for this task | **Justified no-op** | an ADR is the deliverable; approval gate pending (T00-AUDIT row 30, "NEW (ADR only)") |

## Owner decisions requested (original questions)

* **D1 Topology.** Bilateral only (recommended) or also a hub? Hub changes the trust model (Decision 1).
* **D2 Consent.** Is "operator YAML on both sides plus a room owner on the sharing side" the right consent (Decision 3), or must a room owner
  on both sides also accept?
* **D3 Peer authentication.** Pinned-key request/envelope signing (recommended), mutual TLS, or a bearer key plus signing. Also rotation
  cadence and the revocation path.
* **D4 Remote humans.** Shadow `peer-user` principals read/post only (recommended), or no remote humans at all (peer agents only)?
* **D5 Remote reviews.** Never count (recommended default), or allow listing a remote reviewer group in `control_plane.review_quorum`? The
  latter needs the shared vectors changed first, in AutoSpec as well (C7).
* **D6 Retention and unlink.** On unlink or revocation, must the importer purge the mirror, hide it, or keep it read-only for audit? The
  exporter cannot enforce deletion on the peer; is a contractual obligation enough?
* **D7 `ORG_ID`.** Keep "default" (recommended) and give peers a separate namespace, or introduce real organisation ids first?
* **D8 Versioning.** Add envelope v3 with an `origin` block, or carry origin in `evidence_refs`/`claimed_*` and stay on v2? Mixed-version
  peers are rejected above v2 today.
* **D9 Scope of P4.** Are typed cross-boundary requests wanted at all, or is read-only federation the whole feature?
* **D10 Where it runs.** In the combined server only, or also in the isolated `--collab-only` process (which has no graph, so lineage
  federation cannot run there)?

## Owner decisions: resolved

Recorded 2026-10-25: the owner accepted the recommendation for every decision below.

| # | Decision |
|---|---|
| D1 | Bilateral only; no hub. |
| D2 | Operator YAML on both sides plus a room owner on the sharing side only. |
| D3 | Pinned-key signing of requests and exports; rotation cadence about 90 days; revocation by removing the peer in YAML. |
| D4 | Remote humans are shadow `peer-user` principals, read and post only. |
| D5 | Remote reviews never count toward local quorum. Any change needs the shared vectors changed in AutoSpec first. |
| D6 | On unlink or revocation the mirror is hidden by default; an operator purge command deletes it. The exporter cannot enforce deletion on the peer. |
| D7 | `ORG_ID` stays `"default"`; peers get a separate namespace. |
| D8 | Stay on envelope v2; origin is carried as `claimed_*` provenance. |
| D9 | Read-only federation first. Typed cross-server writes (message, artifact proposal) are phase P4 and need a separate approval. |
| D10 | Combined server only; not `--collab-only`. |

Additional P1 requirement: a mirrored `peer:` node or edge must never mark a local gate stale. The fork-promotion staleness derivation
(`control_plane/graph.py`, herdr#264) reads `supersession` edges; P1 adds a test that a mirrored edge cannot trigger it.

## Consequences

* Positive: partners and sites can share evidence without sharing an authority; each side keeps revocation and autonomy; the design reuses
  the ledger, redaction, quarantine and budget machinery rather than adding a second store.
* Negative: a visibility clamp and a new principal kind are real security work (P1) that must land and be reviewed before any network code.
  Eventual consistency and `stale` mirrors are visible to users. A peer can retain what it already pulled.
* Revisit this ADR if D1 (hub) or D5 (remote quorum) changes, or if multi-tenant `ORG_ID` is pursued.
