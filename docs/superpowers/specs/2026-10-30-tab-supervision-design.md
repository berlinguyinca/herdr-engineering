# Tab supervision: base requirement

Each managed Herdr tab represents exactly one durable job with exactly one root agent.
A supervisor that requests a child becomes its immutable supervisor, recursively.
Human ownership remains authoritative. Layout, focus, pane closure, runtime readiness,
and an agent saying done do not establish ancestry or a verified outcome.

## Approved independent seam

`control_plane/tab_supervision.py` is a synchronous policy/event projection using
`DurableEventSink.record_required`. It adds no database, HTTP endpoint, launcher,
process control, credentials, task store, or runtime. Existing collaboration tasks
remain authoritative: `task_id` is a reference, not copied task content. A dedicated
`tab_supervision` event stream can be loaded by the existing central event store.
The root/operator must provide one serialized writer across processes and hydrate
from the complete authoritative stream before accepting commands. An in-process
lock alone cannot coordinate multiple operators. Persistence failures apply no state.

Agent IDs and supervisor IDs survive changing runtime sessions and pane bindings.
A binding names server, runtime session, runtime instance, adapter, and optional
pane. Internal subagents have a real runtime instance and no invented pane ID.
Bindings are explicit operator-authorized changes; no geometric inference or
implicit reparenting. Historical runtime and pane identities remain reserved to
the stable agent even after rebinding or completion; a future attested release
contract is required before another agent can reuse them. Identical binding
receipts are idempotent and do not reset activity clocks. This seam deliberately does not support reparenting.
Historical events retain prior bindings. Closed tab/pane identities remain reserved;
creating a replacement tab requires a later explicit job migration contract.

## Authority and resource contract

Authenticated actor objects are constructed only by the trusted operator after
checking current collaboration grants. They are not accepted directly from model
or HTTP payloads. Human actors control only their own jobs; agent actors control
their own active descendants, and child creation uses the caller as supervisor.
Every command checks the complete current ancestry. Revoked/stopped ancestors cannot
be bypassed by replaying a child binding or creating a new runtime session.

Scopes are opaque canonical grant IDs, compared by exact subset; this module does
not interpret paths or grant capabilities. Allowed models are exact family/model
keys supplied by the existing model policy. Requested scopes/models, token ceiling,
and absolute deadline can only narrow the authenticated parent's current authority
and persistent policy. Usage is charged to every ancestor atomically; siblings
cannot multiply the parent token allocation. Budgets are subtree ceilings, not
reservations: concurrent children may exhaust the shared parent ceiling before an
individual ceiling. The existing user-wide rolling limiter remains independently
required: 10M input+output tokens/hour, finish-only 20% allowance, 12M absolute ceiling.
No job-local ceiling grants the finish allowance or bypasses that limiter.

## Progress, recovery, cancellation, and verification

Checkpoint events carry monotonic progress numbers and bounded evidence references;
heartbeats alone do not reset the progress clock. Reconciliation compares explicit
bindings to a trusted live inventory: absence is orphaned, stale heartbeat is stalled,
and fresh heartbeat without progress is drift. These are observations for supervisor
review, never automatic completion, automatic termination, or identity adoption.
A restart replays all history including stop intents and outcomes. Snapshot/clone
imports cannot authorize operations; only authoritative events and current grants do.

Cancellation records a cooperative stop request for the entire owned subtree,
including queued children. The module performs no signals or pane closure. An adapter
must reconcile delivery receipts and stop acknowledgments after reconnect. Later hard
termination requires elapsed grace and fresh runtime ownership/lease attestation of
exact workload identity, never process name, geometry, arbitrary PID, or shared pane.
Human steering/holds must be checked in the operator before intervention.

Only bound/running agents can be verified; queued work can be cancelled with
cleanup evidence but cannot be marked successful. Checkpoint timestamps come from
the trusted injected clock, never a client timestamp field.

Agents report progress, not final truth. Only their supervisor or owning human may
finalize, with an evidence reference to externally validated result/integration or
runtime cleanup receipt; descendants must already be terminal. Cancelled outcomes
require a prior stop intent. Pane loss and native idle/done are never final outcomes.

## Integration decisions retained for root

1. Extend existing collaboration/operator transaction boundary to authorize calls,
   persist events, publish progress, and invoke existing helper/runtime launch path.
2. Persist a child request BEFORE pane creation; retry using the same child ID and
   reconcile ambiguous launch receipts. Persist binding only after verified launch.
3. Existing helper `successor` uses requester/predecessor semantics that differ from
   supervision. Do not auto-convert it into supervisor inheritance; explicitly decide
   whether a successor is a new child or a resumed binding of a stable logical agent.
4. Add Pi first, then Codex/Claude/custom adapters behind the same trusted boundary.
   Registration never grants launch authority. Pi sandbox, broker-only networking,
   resource attestation, runtime leases and token accounting remain root-owned.
5. Wire rolling user budgets, usage receipt deduplication, cooperative stop delivery,
   attested hard stop, queue dispatch, human override, and UI into existing services.
   This checkpoint enforces policy only; it is not deployed supervision.

## Acceptance

Test exact tab/job uniqueness and sole root; authenticated recursive parentage;
no cross-owner/job authority; immutable ancestry under rebinding; internal subagent
identity; current-grant narrowing; sibling usage charged to ancestor ceilings;
no state change on durable write failure; authoritative replay with contiguous
sequence and schema validation; detached result copies; orphan/drift/stall detection;
cancellation replay; self-completion refusal; evidence and child integration gates.

## Repository and upstream evidence

- `control_plane/collab.py`: `request_helper` binds parentage to authenticated caller,
  `_effective` recursively bounds current helper grants, `_commit` persists before
  applying, `hydrate` requires contiguous history, and `create_task` ties agent
  delegation to an accepted human-rooted task. This module adds tab ownership and
  supervision policy without replacing those checks.
- `control_plane/sink.py`: `DurableEventSink.record_required` acknowledges the
  existing central event store or raises. Use this protocol directly.
- `herdr_adapter.py`: `NativeHerdrAdapter.snapshot/list_agents` already exposes native
  agent session and pane identities. A future adapter must discover actual bindings
  from those responses, not generate them from tree position.
- Installed Herdr 0.9.1 schema (protocol 22, schema 1), queried read-only via
  `herdr api schema --json`: `TabInfo` has layout/state, not job/supervisor linkage;
  panes have optional native agent session references. Feature-detect capabilities.
- Official [Herdr automation](https://herdr.dev/tui/docs/agent-automation/) documents
  pane IDs changing on moves; [session restoration](https://herdr.dev/tui/docs/session-state/)
  distinguishes persisted layout/session references from process survival;
  [concepts](https://herdr.dev/tui/docs/concepts/) distinguishes ready/done UI state
  from task validation.
- Local upstream Pi source revision `307e3b0f4b5f0f4c6a0cb305e54d15a3d2e7370f`,
  `packages/protocol/src/schemas.ts`: `SessionMetadataSchema` has `parentSessionId`,
  but `CreateCommandSchema` has no parent field; `AbortCommandSchema` targets an
  explicit `sessionId`. `packages/server/src/types.ts` exposes runtime prompt,
  steer, abort and snapshots, not a child-session management API.
  `packages/client/README.md` describes exclusive mutation leases.
  No Pi or Herdr lifecycle API is called by this module.

## Operational limits of this checkpoint

`Actor` is a trusted in-process input. Its scopes/models must already reflect the
intersection of live grants through all ancestors; the module checks those current
resolved grants against the target's policy while inspecting persistent ancestry
for stop/deadline and budget checks. Historical actor grants are audit data only.
The sink must be the central authoritative history: a clone cannot be used as a
launch authority. Evidence references are not automatically verified by this module;
only the authenticated supervisor/human, after external verification, calls conclude.

Tab keys are explicit opaque server-qualified registry identities (examples in tests
use `server/tab`); an adapter must preserve the real native tab ID in that registry.
Each binding explicitly repeats the owning tab key and cannot migrate to another
job's tab. Binding server/session/instance/pane facts must be attested by the trusted
operator. Public read views here are internal operator views, not authorized HTTP
responses; existing API authorization must gate any later exposure.
