# Lineage continuity: operator runbook

Scope: the Herdr side of the lineage/continuity work (spec `docs/specs/herdr-lineage-assistant-integration-20261028-20261029T034105Z/`).
Pi-side behaviour (rollover, checkpoint) lives in `berlinguyinca/pi-engineering#460`. Everything here is opt-in and
reversible; nothing in this document needs an environment variable (YAML only).

## What exists, and what does not
| Piece | State |
|---|---|
| Lineage graph, scopes, replay | existing, unchanged semantics |
| `read_lineage` assistant tool | merged in #365, behind `personal_assistant.enabled` and a per-assistant grant |
| Artifact nodes in the graph | commit route, the render worker (HTTP poster -> `/artifact-jobs/<job>/lineage`) and the mail route emit them; ACL read gate narrows visibility; retract and backfill routes below |
| Email attachment -> artifact | `POST /api/v1/artifacts/email-attachments` calls `intel_attachment_artifact.attach_to_artifact` (see "Artifact routes" below). **Nothing in the fleet calls that route yet**: `GmailSync` has no production constructor and its client protocol cannot fetch attachment bytes, so a person or script posts the message and bytes |
| CI failure -> repair -> re-review | graph `validated` edges feed `decide_merge` through `graph_merge_gate` (YAML `control_plane.graph_merge_gate`, off by default); the production repair loop itself is AutoSpec's (not changed here) |
| Delivery metrics | in the room analytics read (`delivery` key). **Not** exposed to the assistant tool, on purpose: see "Why the assistant cannot read delivery metrics" |

## Restart and replay
The graph is a projection over stored `lineage` envelope rows. On start the repo rebuilds it (`ControlPlaneRepo._rebuild_graph`,
`repo.py`); duplicated or out-of-order rows give the same graph. Two things are NOT in the stored rows and are re-created by
the server on every start:
* the artifact ACL read gate (`ControlPlaneServer.__init__` installs it when an ACL is configured);
* room/mission membership (read from the collaboration service at request time).
After a restart check, with a key that holds an ACL grant:
`GET /api/v1/graph/nodes/artifact:<id>` answers 200, and with a key that holds none answers 404.
If the ACL is not configured (`artifact_acl=None`) artifact nodes are emitted `private` (committer only) and no gate is installed.

## Recovery from an interrupted handoff
Herdr never drives a handoff; it only shows one. When a handoff is interrupted (the successor never reports):
1. Read the handoff edge and the successor: `GET /api/v1/graph/nodes/session:<successor>`; the room analytics
   (`/collab/rooms/<room>/analytics`) show `delivery.handoffs.outcomes` with a `pending`/`failed` count.
2. A successor stuck without a state shows as `pending` and contributes no recovery latency (it is never estimated).
3. Recovery is a Pi action (resume from the last verified checkpoint, pi-engineering#460). Herdr records the new `handoff`
   edge when Pi emits it. Do not edit graph rows by hand; emit a corrected event from the owning key, or an operator `edge.remove`
   with a reason (history is kept).
4. Closing a tab or pane never changes agent state (contract rule); do not read a closed UI as a failed handoff.
Known gaps (Pi side, listed in the ledger): no verify-before-revoke ordering, no runaway-process termination, no crash-at-each-stage
tests. Until those land an interrupted handoff needs a human decision.

## Artifact visibility, grants and revocation
* A grant (`POST /api/v1/artifacts/<id>/acl {principal_id, permissions}`) with `read` shows the artifact node and its version
  nodes to that principal immediately. A revoke or an expired grant hides them on the next read, also after a restart.
* The graph label of an artifact event is stamped once and, for a shared (commons) node, only ever widens. So the label is
  `org` (fleet keys only; never a signed-in person) and the ACL gate does the narrowing. Nodes committed before this change are
  `private` (committer only); `POST /api/v1/artifacts/<id>/lineage/backfill` (admin) re-emits their stored versions under the
  gated scope, see "Backfill" below. Without it, committing a new version also exposes the artifact.
* A signed-in person sees an artifact node only when the ACL names exactly `user:<id>` AND the node carries a room/mission label
  that person's membership covers. The commit route does not know the room, so it never emits such a label; callers that do
  (`artifact_scope.visibility_fields(gated=True, room_id=..)`) get both checks. No such caller exists yet.
* Writing artifact nodes needs an ACL `edit` grant (admission gate); a read-only grantee or stranger gets 403 `lineage_forbidden`.
* Known limits from review, not fixed: `issue_lead_time` does not flag a clipped timeline per figure (only the top-level `timeline_truncated`);
  `_same_identity` ignores the key role in the ACL id; HTTP also honours a record's `created_by` and legacy ungranted reads, the graph
  gate uses explicit grants only (stricter); `rework.superseded` includes artifact-version supersessions; `install_gate(acl=None)` does not clear an old gate.
* Retraction now HIDES (see "Retract" below). `artifact_lineage.emit_retraction` alone is only the graph state marker
  (version stays readable by its audience); the HTTP route does the hiding and writes that marker as an audit trail.

## Artifact routes (all authenticated, all through the production gates)
All are `POST` and need an operator-role key (the default for artifact mutations). Rows below say what else is required.

### Retract: `POST /api/v1/artifacts/<id>/retract {"version_id"?, "reason"?}`
* Needs ACL `admin` on the artifact. Body keys other than `version_id` and `reason` are a 400; an unknown version is a 404.
* Effect: the artifact (or that version) disappears from EVERY graph read at the next read, for everyone (admins and owner
  included; the registry still has it): node, version nodes and their `supersession`/`produced` edges (an edge needs both
  endpoints). It also holds for as-of/replay reads and after `_rebuild_graph`, because the gate is evaluated against the
  current projection. Retracting a version that is the head the logical node shows hides the logical node too (it carries that
  version's id and digests); a later correction replaces the head and the logical node reappears for the ACL readers.
* Durable: the registry keeps it (`artifacts.lifecycle='retracted'` for an artifact, `artifact_versions.state='retracted'` for a
  version; no migration, both are free-text columns) and `cli.py` reloads it into the gate before the server serves a read
  (`ArtifactStore.load_retractions`). Without a registry (in-memory server) the hide lasts until restart: the response says
  `persisted: false`. The route hides FIRST and persists second: if persistence fails you get a 500 that says "hidden, but not
  persisted" and the node stays hidden.
* A retracted artifact refuses new versions (409) on the commit route, the worker lineage route and the mail correction route.
  There is no un-retract route: it is a one-way operation by design; to restore, commit a new artifact.
* Nothing is deleted: the stored versions, blobs, ACL rows and the event log remain. The graph also gets a bounded
  `retracted` state marker (audit trail).

### Backfill: `POST /api/v1/artifacts/<id>/lineage/backfill`
* Needs ACL `admin`. For nodes recorded `private` before artifact scoping: re-emits each stored version, oldest first, under
  the gated scope (`org`) and a new event source (`artbf-...`), so ACL readers see the history. Idempotent (a second run is
  deduplicated); skips retracted versions and retracted artifacts. The caller needs the write gate's `edit` (admin implies it).
* It widens the LABEL only; the ACL gate still decides who reads. Run it per artifact; there is no bulk "all artifacts" route.

### Render worker: `POST /api/v1/artifact-jobs/<job>/lineage {"version_id"}`
* The worker (`herdr-eng cp-worker`) holds a Postgres connection and the object store but no graph channel. After a durable
  commit its `ArtifactRenderRunner` calls an HTTP poster built from the worker's `config.yaml`:
  `control_plane.artifact_worker.lineage: {api_url, key_file, timeout_seconds}`. `key_file` is a file path (the key is never
  inlined in YAML or read from the environment). Absent section = no poster = the old behaviour.
* The server reads the version from the registry; the body carries only `version_id`. It requires: the job exists, the caller
  holds `edit` on the job's artifact (give the worker key an ACL `edit` grant, or let it be the owner), the version belongs
  to that artifact AND carries that job id, and the artifact/version is not retracted. Replays are deduplicated (idempotent).
* A graph failure is a 502 and the worker logs it and moves on (the version is already durable; use Backfill to repair).

### Mail attachment: `POST /api/v1/artifacts/email-attachments`
* Body: `message` (the Gmail message object: id, threadId, payload), `part_path`, `account`, `data_base64`, `audience` (list of
  ACL principal ids that may READ), optional `previous: {artifact_id, version_id}` for a correction. The owner is always the
  authenticated caller (a body `owner`/`created_by` is a 400); readers get `read` only. Size cap 25 MiB decoded.
* Triage decides first: quarantined/rejected bytes are never stored (422, reasons only). A redelivery of the same
  (account, message, part, sha256) answers 200 `duplicate`; that guard is in memory and is lost on restart (a redelivery after
  a restart creates a second artifact). A correction needs `edit` for the caller and cannot add readers (409).
* Issue links appear only when project intelligence is enabled and a person confirmed the relationship for that message.
* Config: nothing new; it needs the ACL, the registry and the object store that the other artifact routes need.

## Graph evidence in the merge gate (`control_plane.graph_merge_gate`)
In the central fleet file's `control_plane` section (the same file that carries `review_gate` and `project_intelligence`):
```yaml
control_plane:
  graph_merge_gate:
    enabled: true                  # default off; false (or removing the section) is a hot kill switch
    evidence_writers: ["key:<12 hex of the CI reporter key>"]   # required; only these writers' edges count
    required_checks: ["ci"]        # edges name their check in attrs.check, default "ci"
    require_review_quorum: false   # true also needs the head-bound review quorum (control_plane.review_quorum)
```
* Runs for every PR after `decide_merge` (and the repair policy) allowed a merge, so it can only withhold. A `validated` edge
  counts only if written by an evidence writer, bound (server-validated `binding.commit`) to the PR's exact head SHA, not stale
  and not removed. Anything else is reported as "stale evidence ignored" and never passed on as a check: after a push the
  required check is missing and the PR waits (a pending status), it is not rejected. An unknown conclusion is "pending".
* Fail closed: a bad section, a missing PR node or any exception is `wait`. Once enabled, an unreadable central config also waits.
* Reading needs the evidence to be `org`-visible or wider (private edges are invisible to the gate's reader by design).

### Turning the graph merge gate on (operator steps)
The shipped example (`deploy/control-plane/fleet-config.example.yaml`) carries the block COMMENTED and `enabled: false`. Nothing
is enabled by deploying it.
1. Find the principal id of the CI reporter: `key:` plus the first 12 hex of `sha256(<its API key>)` (the same id the server
   records as `provenance.writer`). The gate reads only edges whose writer is in `evidence_writers`; an agent cannot vouch for
   its own CI.
2. Uncomment the block in the central fleet file, replace the placeholder writer with that id, set `enabled: true`.
3. `herdr-eng fleet config validate` (and control-plane startup) validate the block whenever it is a mapping, enabled or not, and refuse: a
   non-boolean `enabled`, unknown keys (a typo like `enabld`), a wildcard writer, and, when enabled, an EMPTY `evidence_writers`; a wildcard (`*`, `key:*`) or anything that is not `<kind>:<id>` (so the placeholder itself is refused).
   An enabled-but-invalid section never leaves the gate silently open: startup stops, and at run time the gate answers `wait`.
4. What writes `validated` edges: a CI reporter posting a lineage `edge.add` of type `validated` (`from` = the check/run node,
   `to` = `pr:<repo>#<n>`, `attrs.commit` = head SHA, optional `attrs.check` and `attrs.conclusion` such as `success`) with the
   evidence writer's API key. The server stamps `provenance.writer` and the binding; the gate trusts only those. Edges must be
   `org`-visible or wider to be seen. Until the CI reporter writes them, every PR waits with `graph evidence: ...` pending.
5. Roll back by setting `enabled: false` (hot; no restart) or removing the block.

## Why the assistant cannot read delivery metrics
`lineage_metrics` is computed from a collaboration ROOM snapshot (room membership, the room's evidence writers, k-anonymity
suppression). The assistant's principal is an API key, which has no room membership; there is no reader-scoped way to build that
snapshot from it, and building one from a contexts list the model or server supplies would widen visibility. So `read_lineage`
has no metrics operation (a test pins that). Members read metrics from the room analytics route.

## Assistant approvals, tenants and audit (YAML)

```yaml
control_plane:
  personal_assistant:
    tenancy:
      default_tenant: default          # principals not listed below
      principals:                      # authenticated principal id (key:<12 hex of sha256(api key)>) -> tenant
        "key:0123456789ab": acme
    actions:
      approvers: ["key:..."]           # may decide approvals/proposals in their OWN tenant
      auto_execute: true               # run an action/proposal as soon as it is approved (false = POST .../execute)
```

* Tenant is never read from a header, query or body. A malformed tenant id stops startup.
* `POST /api/v1/assistant-approvals/<id>/execute` and `POST /api/v1/assistant-proposals/<id>/execute` run what was
  approved, once (replay returns the recorded result; a failed action is not retried under the same key). Each
  re-checks tenant, approval, expiry, grant and the payload hash. `send_message`, `run_shell` and `edit_shared` have no
  executor: approving them records permission only.
* `GET /api/v1/assistant-audit` reads the audit log (`chain_intact` reports the per-tenant hash chain). It is stored
  as `assistant_audit` events in the repo event store, written before the operation completes; there is no route to
  change or delete an entry. Approved actions and proposals write an `...executing` audit entry BEFORE their side
  effect, so with the event store down nothing is run and the action stays approved. Other operations error when the
  entry cannot be written, but the state change made just before it may already be stored.
* Audit integrity limits. Without a key the chain is an unkeyed SHA-256: it detects casual edits and a removed or
  reordered entry in the middle, but anyone who can write the event store can recompute it, and truncating the tail
  or rolling the stream back still verifies. Set `personal_assistant.audit.hmac_key_file` to the PATH of a file (at
  least 16 bytes, outside the repo, readable by the control plane; a missing or short file stops startup) and entries
  are HMAC-SHA-256 keyed, so an edit cannot be re-signed without the key. Truncation or rollback is only detectable
  against a head recorded elsewhere: `GET /api/v1/assistant-audit` returns `head` (`seq`, `sha256`); keep the latest
  one off the box and compare. Rotating the key makes older keyed entries unverifiable by the new key.
* Off-box anchoring of the head: see "Anchoring the audit head off the event store" below.
* After an effect (review request, comment, draft, connector call, hand-off) a failing audit store does not turn the
  result into an error: the action stays `completed` with `result.audit_pending`/`audit_error`, the error is logged,
  and the entry is written on the next replay of `.../execute` or the next `GET /api/v1/assistant-audit`.
* A queued action keeps its request payload in memory so it can run; the persisted copy has secret-looking values
  scrubbed. An action restored from a snapshot that had to scrub a secret is refused (`not persisted`) rather than
  sent with a placeholder: request it again. Content that is secret but does not look like one (free text) is stored.
* Audit before effect is not universal: only `execute_approved_action` / `execute_approved_proposal` write the `executing` entry before the effect. The inline connector `/request` and `/test` path and the chat `_execute_gated` path record the call afterwards, so they can run while the audit store is down.
* If the audit sink commits but then raises (for example a timeout after commit), the entry is durable but absent from memory and the next entry reuses its `seq`; after a restart the chain then reports `chain_intact: false` until repaired. The hydrate order is by timestamp, so a backwards clock step can reorder entries. The sink write happens under the store lock, so a slow database slows every assistant operation. Not fixed in this change.
* Moving a principal to a new tenant in `tenancy.principals` hides what it created under its old tenant (`default`):
  assistants, memory, approvals and notifications are tenant-scoped. Re-create them or keep the mapping.

## Rollback
All changes are additive; roll back in this order, each step safe on its own:
1. Switch the assistant tool off: `personal_assistant.enabled: false` (or revoke the `read_lineage` grant).
2. Revert the artifact-scope PR: artifact nodes are written `private` again and the gate is gone. Existing `org`-labelled
   artifact events stay in the log and, without the gate, would be visible to every fleet key. **Before reverting, re-label or
   accept that exposure**: the safe alternative is to leave the gate code in place and revoke grants.
3. Revert the stack (#365 base last). No migration was added; no schema changes. Retraction rows in `artifacts.lifecycle` /
   `artifact_versions.state` stay in the registry harmlessly.

## Feature flags and grants (YAML)
```yaml
control_plane:
  personal_assistant:
    enabled: false          # default; true exposes the assistant and its tools
  collab:
    insights:
      enabled: true
      evidence_writers: ["key:<12 hex>"]   # graph principals whose merged/failed states count; empty = metrics say "unknown"
      min_group_size: 5                    # figures over fewer items are suppressed
  lineage_key_scopes:       # which rooms/missions a key may address (see lineage-events.md)
    "<ingest key>": {rooms: ["<room id>"], missions: ["<mission id>"]}
```
Assistant tool grants are created per assistant (Settings -> Permissions, or the assistant grants route); `read_lineage` is
not sensitive but still needs a grant; there are none by default.

## Metrics
`GET /api/v1/collab/rooms/<room>/analytics` now carries `delivery` (reader-visible items only, no per-person or per-agent
breakdown): accepted changes, gate failures, handoff success and recovery latency, rework, review findings, issue lead time,
sessions, tokens and cost. Tokens and cost are summed only from nodes whose emitter reported `tokens` / `tokens_in` /
`tokens_out` / `cost_usd`; otherwise `status: unknown`. Accepted changes and gate failures are `unknown` until `evidence_writers`
is configured. Times are server receipt times.

## Cross-repo notes (AutoSpec; not edited here)
* The shared review-quorum vectors are byte-identical (sha256 `18d82766...6f39`); a test pins the digest. Any rule change is a
  two-repo change: change the vectors in AutoSpec first, copy them, update the digest in
  `tests/control_plane/test_ci_repair_rereview_gate.py`.
* The merge decision for a repair loop is AutoSpec's `review-quorum` plus the check state; Herdr only projects it. AutoSpec would
  need to emit a `validated` edge per CI run with `attrs.conclusion` (`pass`/`fail`) bound to the exact commit, and a `produced`
  edge from the repair attempt to the PR, for the Herdr read model (and its author exclusion) to see a repair author.

### Queued delegations: the consumer (`personal_assistant.delegation_consumer`)
```yaml
personal_assistant:
  delegation_consumer:
    enabled: false          # default off; must be a real boolean (a quoted "false" stops startup)
    source_harness: pi      # recorded as the handoff's source (pi|claude|codex|opencode)
```
* An approved `request_mission` / `propose_branch` becomes a `queued` delegation (kind `mission` / `branch`). With the
  consumer on, `POST /api/v1/assistant-delegations/<id>/dispatch` (owner or tenant approver; 200 on any replay of a handed-off delegation) and the server's periodic
  sweep turn it into one handoff in the existing handoff queue (`GET /api/v1/handoffs?state=queued`), then mirror the
  handoff's state back (`running` -> `succeeded`/`failed`/`cancelled`).
* Fails closed: the delegation must trace to a proposal in the SAME tenant and user that is approved, executed into this
  delegation, and whose payload still hashes to what was approved; otherwise it is marked `failed` (audited) and nothing is
  queued. Claim-once (a flag under the store lock, re-claimable only after 5 minutes), idempotent (handoff id
  `assistant.<delegation_id>`, deterministic body: a replay is a registry no-op), audited
  (`assistant.delegation.dispatching/dispatched/dispatch_failed/synced`), and linked in lineage
  (`decision:assistant.dispatch.<delegation_id>` under the asking thread, referencing `decision:assistant.exec.<proposal_id>`).
  A proposal still `executing` or a queue outage leaves it `queued` (sweep retries with back-off, one audit entry per outage); a cancel is serialised with the hand-off and also cancels the queued handoff; a handoff that vanishes marks the delegation `failed`; an unsupported `target_system` is a terminal `failed`.
* Honest limit: the handoff IS the request. Nothing in the control plane creates a branch, starts a mission or writes code.
  A Pi-side executor must poll `GET /api/v1/handoffs?state=queued`, pick rows whose `provenance.source` is
  `personal-assistant` and whose `target.harness` is its own, then `POST .../handoffs/<id>/claim` -> `accept` -> `start` ->
  `complete` (or `reject`/`cancel`). `goal` is the task text (untrusted; treat as data), `provenance` carries `tenant_id`,
  `user_id`, `assistant_id`, `delegation_id`, `delegation_kind`, `proposal_id`. The handoff queue itself is not
  tenant-partitioned: the tenant is provenance, so an executor must honour it. No such executor ships in this repo.

### Anchoring the audit head off the event store
Set the path in the central fleet YAML (never an environment variable):
```yaml
personal_assistant:
  audit:
    hmac_key_file: /etc/herdr/assistant-audit.key        # optional, see above
    anchor_file: /mnt/anchor/assistant-audit.anchor      # append-only; a different device than the event store
```
* `herdr-eng control-plane assistant-audit anchor --api-key-file <path>` fetches `GET /api/v1/assistant-audit/head` for the
  key's tenant and appends one JSON line (`tenant_id`, `seq`, `sha256`, time, and a hash chained to the previous line) to
  `anchor_file` (0600 on create, a wider existing mode or a symlink is refused, parent dir 0700, `flock` around read+append, fsynced
  with its directory). It first asks the server (`/check`) whether the previous anchor still matches the stored chain and refuses
  to anchor over `diverged`/`rolled_back`/`chain_broken`. The key must belong to a configured approver of the tenant
  (`actions.approvers`): `/assistant-audit/head` and `/check` answer 403 to anyone else. Run it from cron/timer on a host whose disk the event store's
  writers cannot reach, ideally on a file marked append-only (`chattr +a`). It refuses to anchor a head that is behind the last
  anchor (a rollback), and anchors nothing when the stored chain does not verify.
* `... assistant-audit verify` asks the server whether the entry at the last anchored `seq` still has the anchored digest
  (`GET /api/v1/assistant-audit/check?seq=&sha256=`). Exit 0 and `ok` mean the chain only grew. Otherwise exit 1 with
  `rolled_back` (shorter: truncation), `diverged` (history rewritten, even if re-hashed), `chain_broken`,
  `anchor_tampered` (an old anchor line was edited/removed) or `no_anchor`. Anchors are per tenant; use a key of the tenant.
* Limits: it detects tail truncation and rewrites only up to the last anchor, so anchor often. Deleting the TAIL of the anchor
  file itself is not detectable by the file's own chain; keep it on append-only storage or copy it elsewhere. The command is
  run by an operator or timer; the control plane does not run it for you.
