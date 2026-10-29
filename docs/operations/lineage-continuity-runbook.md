# Lineage continuity: operator runbook

Scope: the Herdr side of the lineage/continuity work (spec `docs/specs/herdr-lineage-assistant-integration-20261028-20261029T034105Z/`).
Pi-side behaviour (rollover, checkpoint) lives in `berlinguyinca/pi-engineering#460`. Everything here is opt-in and
reversible; nothing in this document needs an environment variable (YAML only).

## What exists, and what does not
| Piece | State |
|---|---|
| Lineage graph, scopes, replay | existing, unchanged semantics |
| `read_lineage` assistant tool | merged in #365, behind `personal_assistant.enabled` and a per-assistant grant |
| Artifact nodes in the graph | commit route and render-runner hook emit them; ACL read gate narrows visibility |
| Email attachment -> artifact | control-plane function `intel_attachment_artifact.attach_to_artifact`; **not yet called by any route or job** |
| CI failure -> repair -> re-review | covered by a test over existing pieces; the production loop is AutoSpec's (not changed here) |
| Delivery metrics | in the room analytics read (`delivery` key); the assistant tool does not call it yet |

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
  `private` (committer only) and stay that way; there is no in-place widening. To expose an old artifact, commit a new version.
* A signed-in person sees an artifact node only when the ACL names exactly `user:<id>` AND the node carries a room/mission label
  that person's membership covers. The commit route does not know the room, so it never emits such a label; callers that do
  (`artifact_scope.visibility_fields(gated=True, room_id=..)`) get both checks. No such caller exists yet.
* Writing artifact nodes needs an ACL `edit` grant (admission gate); a read-only grantee or stranger gets 403 `lineage_forbidden`.
* Known limits from review, not fixed: `issue_lead_time` does not flag a clipped timeline per figure (only the top-level `timeline_truncated`);
  `_same_identity` ignores the key role in the ACL id; HTTP also honours a record's `created_by` and legacy ungranted reads, the graph
  gate uses explicit grants only (stricter); `rework.superseded` includes artifact-version supersessions; `install_gate(acl=None)` does not clear an old gate.
* Retraction (`artifact_lineage.emit_retraction`) marks a version `retracted`; the version and its digests remain readable
  by the same audience. There is **no HTTP route** for it yet.

## Rollback
All changes are additive; roll back in this order, each step safe on its own:
1. Switch the assistant tool off: `personal_assistant.enabled: false` (or revoke the `read_lineage` grant).
2. Revert the artifact-scope PR: artifact nodes are written `private` again and the gate is gone. Existing `org`-labelled
   artifact events stay in the log and, without the gate, would be visible to every fleet key. **Before reverting, re-label or
   accept that exposure**: the safe alternative is to leave the gate code in place and revoke grants.
3. Revert the stack (#365 base last). No migration was added; no schema changes.

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
