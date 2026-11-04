# Target architecture and contracts

## Ownership
Hindsight is canonical persistent **engineering** knowledge. Existing `herdr-memory` is preferred API/client and durable outbox. Pi owns engineering workflow/worker execution and its session-memory logic. Herdr Engineering owns distribution, identity mapping, API access policy, lifecycle event integration, and dashboard. `herdr-handoff` owns handoff packages. `herdr-fleet` owns declarative installation. Rakazo owns personal-assistant source memory and never implicitly shares it with engineering.

## End-to-end flows
1. Agent starts => harness adapter emits identity and task scope to Herdr memory capability => resolve allowed global/project/mission banks => recall bounded ranked evidence with provenance => inject clearly labeled, untrusted reference context.
2. During task => explicit `memory.search` tool optional; automatic recalls only at supported hooks, bounded by latency and context budgets.
3. Significant discovery => candidate evidence event (no raw hidden reasoning) => verification/PR/test => promote fact/decision/lesson via canonical client; idempotency key stable per evidence+scope; acknowledge only on durable receipt/outbox commit.
4. Handoff => existing handoff package references memory IDs, revision, bank scope and retrieval snapshot digest; do not copy secret or full memory contents. If offline, write durable local checkpoint and mark remote sync pending.
5. Dashboard => existing control-plane read API authorizes actor and scopes, queries canonical client, enriches with Herdr existing links; emits audit metadata. No client-side direct Hindsight credentials.

## Stable contracts (version 1, proposed)
- Memory identity: `memory_id`, `bank_id`, `revision`, `kind` (verified_fact|decision|preference|hypothesis|failure|procedure), `status` (candidate|verified|superseded|retracted), timestamps, `valid_as_of`, `supersedes_id` optional.
- Provenance: `source_system`, `actor_id`, `harness_id`, `host_id`, `repo_id`, `mission_id`, `session_id`, `execution_id`, `commit_sha`, `pr_ref`, `evidence_refs` (sparse optional fields). Do not invent missing IDs or infer graph edges.
- Recall request: `query`, `scope_context`, `max_results`, `max_tokens`, `include_candidates=false`, `as_of` optional; response contains source bank/ID/revision, text, provenance, record status, filter/ranking explanation if server can provide, and timing.
- Retain request: typed content, source evidence and explicit target bank; outcome `persisted | queued_durable | rejected`; never claim persisted for queued or rejected.
- Correction: versioned supersession/tombstone with ACL and provenance, and cache invalidation.
- Tool surface: `memory.recall`, `memory.retain`, `memory.reflect` (when supported), `memory.get`, `memory.explain`, `memory.correct`, `memory.feedback`; distinguish native server methods from Herdr wrappers and omit unsupported operations in first slice.

## Bank policy
- Global: non-sensitive broadly reusable engineering policies; promotion restricted.
- Infrastructure: verified time-sensitive host facts, `valid_as_of` and revalidation.
- Project: repo/project decisions and recurring lessons.
- Mission: work-in-progress and bounded durable mission checkpoints.
- Session: private/candidate context, not automatically globally shareable.
- Personal-assistant/Rakazo: separate identity/store; no cross-domain browse or default promotion.
Bank hierarchy is implemented by Herdr scoped query composition, not assumed native Hindsight inheritance.

## Security
- Server-side permission checks before recall and on every referenced memory detail; deny by default.
- Separate read/retain/promote/admin privileges and separate machine identity from model-generated claims.
- No raw full transcripts or hidden chain-of-thought in memory. Scrub credentials and PII. Retrieved memory always labeled untrusted data, never a system instruction.
- Restrict memory sources to tenant/project membership; don't let user-provided bank IDs bypass ACLs.
- For current assistant MCP hub, preserve personal scope and use a separate engineering worker grant/route if necessary.
- Audit access metadata without logging sensitive memory text.

## Performance and availability
- Preserve Pi's documented 250 ms automatic lookup budget / 60 sec cache / fail-open behavior initially, benchmark before changing.
- Version-scoped cache keys include identity, bank set, memory revision, query and permissions hash; invalidate on correction/deletion.
- Durable outbox + idempotency for writes, bounded disk use, observable retry and dead-letter handling; if queue full report failure instead of silent success.
- Degraded mode: finish task when memory down if task permits; security-critical decisions fail closed to missing policy and are flagged.

## Native harness adapter contract
`detect`, `planInstall`, `install`, `verify`, `status`, `repair`, `uninstallManagedOnly`, `capabilities`. Adapter declares optional native lifecycle hooks, context-injection support, MCP and command support; fallbacks must be explicit. Config overrides are managed overlays with diff and backup, never file replacement. Future harness support implemented behind the same interface.
