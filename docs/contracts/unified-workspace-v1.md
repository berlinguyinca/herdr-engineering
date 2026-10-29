# Unified workspace contracts v1 (P01)

Status: contract + library only. Flag `control_plane.unified_workspace.enabled` (YAML, default off) advertises
capability `unified_workspace_v1`. Schema: `schemas/workspace-v1.schema.json`. Code:
`herdr_engineering/control_plane/workspace_contracts.py`. Spec: `docs/specs/herdr-unified-workspace-20261028/`.

## Identity
`urn:herdr:<kind>:<uuid>` with `uuid5(FIXED_NAMESPACE, "<kind>:<native id>")`. Native ids stay authoritative (HCN
room id, artifact_id, artifact version id, GitHub ref); every host derives the same urn, nothing is derived from a
display name, so a renamed room keeps its urn. `aliases[]` carries `{system, value}` pairs; an `AliasTable` binds an
alias once per tenant and refuses rebinding. Synthetic `new_urn()` (uuid4) is for entities with no native id (team, project).

## Kind to graph.py mapping (nothing in graph.py changes in P01)
| contract kind | graph node type | status |
|---|---|---|
| room, mission, issue, task, agent, host, artifact, review, decision | same name | exists |
| agent_session | `session` | exists |
| project, team | `project`, `team` | ADDED by P02 (owned kinds, not COMMONS) |
| artifact_version | `artifact_version` | ADDED by P02 (a version is immutable and individually linkable) |
| external_action | `external_action` | ADDED by P02 |

| contract edge | graph edge type | status |
|---|---|---|
| assigned_to, implements, produced, references | same | exists |
| depends_on, delegated_to, spawned, reviews, supersedes | `dependency`, `delegation`, `spawn`, `reviewed`, `supersession` | exists (alias, no twin) |
| contains, derived_from, discussed_in, approved_by, notified_via | same | ADDED by P02 |

`EXTENDED_NODE_TYPES` / `EXTENDED_EDGE_TYPES` in the module are what P02 registers. Endpoint kinds per edge are in
`EDGE_ENDPOINTS` (enforced by `validate_edge`, not yet by the live graph).

## Authoritative owners and correlation lookup
HCN CollabService: room, team membership, room task. Pi Engineering: agent, agent_session, mission. Artifact
registry: artifact, artifact_version. GitHub (via alias): issue. Fabric events: event. Correlation lookup is by
`correlation_id` / `causation_id` on events and edges; `trace_id` is optional because `events.py` has no such column today.

## Scope
`visibility` reuses `graph.SCOPE_RANK` keys (`private|mission|room|org`); `room`/`mission` must name their context
(`room_id`, `mission_id`), mirroring graph's contextual label which `can_see` resolves against HCN membership. A bare
room scope is operator-only in the live graph, so the contract refuses it.

## Mutation rules
`If-Match: <etag>` compare-and-swap (428 without, 412 stale); `Idempotency-Key` with request fingerprint (same key and
other payload = 409; external effect with unknown outcome = in doubt, reconcile, never blind retry). Artifact versions and
tombstoned documents are never updated; supersede with a new version plus a `supersedes` edge.

## Broker
`allowed = role actions ∩ agent delegation chain (∩ AGENT_ACTIONS) ∩ room grant ∩ artifact grant ∩ action capability ∩
account grant (shell/external only) ∩ risk policy`. Room membership never produces shell or external-account capability
(`attach_shell`, `send_email` need a user-scoped account grant). CRITICAL risk (`send_email`) always needs a fresh
approval; any non-read-only action derived from untrusted content needs one too and a preauthorized grant cannot
waive it. Approvals are single-use, expiring, bound to the exact request digest and the policy snapshot, issuable only by
the human the action is for, and re-validated at use time (the broker re-authorizes on every invoke).

## Taint
`Untrusted(value, sources)` marks room text, attachments and tool results. Summaries carry the union of their inputs'
sources. Tool output is `Untrusted`. Delegation can never be derived from tainted context.

## ACL hooks
`traverse` (filter before traversal and edge expansion; degrees, counts and `has_more` over the visible subgraph only),
`search` (ACL before match, visible-only totals), `export_items` (also strips invisible references),
`SubscriptionGuard` (re-authorizes every delivery, closes on first denial). Missing and forbidden are one error.

## Subscribe / replay cursor (pinned against `collab.py`)
`read_events(cursor, limit)`: events with `sequence > cursor` (not below the member's join sequence), oldest first,
`next_cursor` = last sequence delivered or skipped-as-hidden, `has_more`. Same cursor, same result. `room_state(since,
cursor, wait)`: `{seq, changed:false}` iff the room sequence still equals `since`; access lost mid-wait is 404.
Viewer-visible sequences are not contiguous, so gaps are repaired by re-reading from the last committed cursor. Client:
`ReplayCursor.apply_page(requested_cursor, page)` advances only on the response to a request issued from the current
cursor, drops `sequence <= cursor`, never regresses. A retention-gap signal (`snapshot_required`) does not exist yet (P02).
