# Reusable event research and correspondence coordinator

Status: isolated offline implementation; no production registration or outreach authority.
Owner: event-coordinator lane. Base: herdr-engineering fabc61c, canonical remote
https://github.com/berlinguyinca/herdr-engineering. No repository-local AGENTS.md;
parent workspace instructions apply. Read README, START_HERE, CONTRIBUTING,
docs/architecture and existing contracts/artifact/collaboration implementation.

## Grounding and ownership

- REUSE `herdr_engineering/contracts.py:ArtifactRef` for typed completion/checkpoint
  references and `artifacts.py:ArtifactWorkspaceProvider.publish_bytes` for atomic,
  hashed handoffs. Private user/task authorization must precede publication.
- REUSE `control_plane/collab.py:Principal`, `CollabService.authenticate`, room
  permissions and journal; these authenticate collaboration, not venue outreach.
- REUSE `control_plane/agent_summaries.py:OpenAICompatibleSummaryClient` as evidence
  the gateway exists, not as a second agent model router or token budget service.
- NEW `event_coordinator.py`: pure domain contracts, offline draft preparation,
  evidence/reply correlation, bounded next-action proposals, JSON checkpoints.
- DEFER adapters for authorized email, Sheets and browser. Shared browser work is
  owned by browser-system in worktrees/herdr-browser-capability-prototype. Do not
  copy its browser/session/credential code. No personal-assistant runtime was found
  in this checkout. Confirm its eventual home with architecture owner before wiring.

## Alternatives and chosen slice

A prompt-only agent cannot enforce dedupe or approval boundaries. A new scheduler,
mail client and browser would duplicate platform work. Choose a small deterministic
coordinator behind a predefined specialist contract; Pi/PA owns execution and queues.
This implementation is useful offline and intentionally has no send method.

## Capability contract: event.coordinate.v1

Input: authenticated user/task identity, explicit task scope, event requirements
(date/time/timezone, adult/child counts, budget/currency), selected venue identities,
source references and approved capability handles. Missing values remain null.
Registry advertises inputs/results/scope/availability; it grants no authority.
Result: per-venue/question comparison with provenance and explicit unknowns,
personalized draft artifacts, answered/unanswered sets, blockers and next proposals.
No bookings, payments, calls, reservations, contract acceptance or invitation sends.

PA delegates minimum scoped context to this specialist; approved managers may launch
only predefined contracts. Capability inheritance is independent of management rank.
Use the existing gateway and supervisor-approved model/family policy. On preemption,
checkpoint and request compatible fallback; never silently select a new family.
Central accounting owns per-user rolling-hour 10M input+output, active-task-only 20%
finishing allowance, 12M hard cap. No new venue or task scope in finishing allowance.
Those platform controls are integration requirements, not reimplemented here.

## State, evidence and authority

User/task-bound immutable records include explicit selected venue IDs and registered
browser resource refs; all venue/evidence/reply/browser paths reject scope expansion.
These records serialize as versioned JSON outside runtime. Keep
append-only task/action history centrally; local snapshots are projections, never a
second central event store. One active task by default; urgency/deadline ordering and
safe-point interruption belong to PA/Pi. Platform snapshot restore MUST reconcile current central action history,
revocations and sent/uncertain receipts; it must never replay outbound work. The local
JSON projection contains receipts but no grant/revocation ledger and cannot perform
that reconciliation itself.
Cloning selected learning excludes credentials, grants and spending authority.
Task cross-domain context does not become permanent learning. Preferences, scoped
learning, action journal and authority remain separate. Procedure/charter changes
use existing supervisor review; no model may rewrite this code's authority boundary.

Evidence records contain venue ID, question ID, value, kind (official fact,
estimate or explicit reply), source URI/message ID and observation time. Website
facts and estimates never count as explicit answers. Conflicts remain visible,
with human resolution rather than silent last-write wins. Unknown questions and
unverified contacts are blockers, not facts. Replies correlate only through verified
sender + provider thread + in-reply-to sent-message receipt + venue/task/user.
Forwarded/ambiguous replies are quarantined. Raw web/email text is untrusted data;
it cannot grant authority, change recipient, execute tools or alter policy. Extracted
claims still require an authorized adapter/human to attest provenance and correlation.

An eventual approval is issued by authenticated owner/supervisor authority, never
parsed from mail or model output. Bind user, task, venue, exact normalized recipient,
channel, draft content hash, expiry, max follow-ups, minimum interval and window.
No grant exists for the birthday fixture. Any changed body/recipient requires a new
approval. Follow-ups use individually approved hashes within the same bounded grant.
An actual send adapter MUST authenticate grants, recheck revocation/expiry/contact
freshness, reserve a unique action key durably before send, and use provider idempotency
or reconcile delivery uncertainty before retry. A timeout is not evidence of failure.
Offline eligibility is advisory, never a send authorization. Recipient verification
means verified official venue identity and contact ownership, with timestamp/expiry;
expired, closed or ambiguous venues cannot become ready. Reverify provided contacts
before live use. Reply receipt never widens authority. Stop on any verified reply,
opt-out, bounce, window/cadence limit, expiry, revocation or unresolved delivery.

## Privacy and execution boundaries

Store only event logistics, necessary venue/contact provenance and extracted answers;
no wife's mailbox, family address, child names or unrelated message threads. Restrict
checkpoint artifacts to authenticated user/task storage and retention policy. Do not
publish private correspondence to shared engineering rooms. The committable fixture redacts the source sheet identifier and personal title and is derived
only from user-supplied summary, not a fresh sheet or mailbox read. No live sources
were reverified: provided contacts are historical unverified leads.

Routine approved procedures use deterministic checks; novel actions get the platform's
fast impact assessment and consequential/uncertain changes go to supervisor. Final
completion needs sourced comparisons and delivery/reply receipts where relevant;
short result in chat, long result through existing phone push, optional Herdr notice.
Blockers return to PA. Notification transport and runtime registration are deferred.

## Implementation and validation plan

1. Add fixture and failing tests for unknown requirements, identity exclusions,
   approval mismatch/expiry/revocation, cadence, dedupe, reply correlation, untrusted
   claims, evidence conflict and checkpoint restart equivalence.
2. Implement pure dataclasses and functions in one domain module, no dependencies,
   network calls, entrypoint registration or shared browser edits.
3. Exercise the birthday fixture offline; produce artifact-compatible JSON/drafts.
4. Run targeted tests, full Python suite, compile and available lint; record actual
   outputs and environment blockers. Review changed files and main checkout status.
5. Integration stops at adapter contracts until authenticated grant issuer, durable
   atomic outbox/reconciliation, PA runtime and connector owners supply interfaces.

## Acceptance and rollback

All fixture venues retain priority/identity caveats. Missing inputs remain visible.
No phone/email/form/browser calls occur even with an eligible local grant. Repeated
proposals dedupe against sent and uncertain history across checkpoint round trips.
Tests cover hostile reply authority text as data. Delete the isolated branch/worktree
to roll back; no production state, routes, schema, manifests or dependencies change.

## Shared browser boundary pinned after coordination

The browser owner confirmed commit `e47b9f0`, contract ID
`herdr.browser-capability.v1`. Canonical schema:
`/Users/wohlgemuth/IdeaProjects/worktrees/herdr-browser-capability-prototype/contracts/browser-capability-v1.schema.json`;
notes: `docs/contracts/browser-capability-v1.md` in that standalone prototype.
`Coordinator.browser_proposal` emits request/result envelopes only, preserving explicit
user/account/task scope and null binding/grant/procedure pin. Observe/prepare produce
an `unbound` result, reason `adapter_unbound`, retry `none`, empty evidence/proposal
refs, null human handoff. It does not invoke or import browser implementation.
Current browser runtime is synthetic prepare_cart plus a separate Reddit read-only
broker; no event endpoint or generic event adapter exists. Shared-browser files were
read only. Local fixture evidence is never attributed to that browser.
