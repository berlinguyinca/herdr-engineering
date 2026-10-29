# Persistent Work Graph v1 (unified workspace P02)

Flag: `control_plane.unified_workspace.enabled` (YAML, default off). Off: every `/api/v1/graph/work/*` route is a 404, the
capability `unified_workspace_v1` is not advertised, migrations 0033/0034 are inert additive tables. On without
`control_plane.pg_dsn`: the routes answer 503 (never a crash). Rollback = flag off; DOWN scripts are in the migrations.

## What it is
A **read model** over the existing sources, persisted in the existing PostgreSQL (no Neo4j, no second event log, no second
database). It is the durable "who/what/why" graph for projects, issues, tasks, rooms, agents, sessions, artifacts and their
versions. `pi_relationship` stays separate (decision D1); the in-memory lineage `GraphProjection` and its routes are untouched
(only the vocabulary gained the owned kinds `project team artifact_version external_action` and edges
`contains derived_from discussed_in approved_by notified_via`).

Sources (all read, none copied): `fabric_events` (`entity_type` `collab`, `session`, `artifact`), the artifact registry tables
(`artifacts`, `artifact_versions`, `artifact_bindings`) and the GitHub catalog (`github_issues`).

## Model (migration 0034)
| Table | Role |
|---|---|
| `wg_event` | ledger of source events already projected (idempotence; the reconciler's anti-join) |
| `wg_assertion` | immutable evidence linkage: one row per statement an event made about a subject, with a total-order `rank` |
| `wg_entity` / `wg_alias` | canonical `urn:herdr:<kind>:<uuid5(kind:native)>` + aliases (`collab_task`, `collab_room`, `github_issue`, `pi_session`, `artifact`, `artifact_version`) |
| `wg_edge` / `wg_edge_interval` | typed edges, unique `(edge_type, from_id, to_id)`, every edge has `source_event_id`; validity intervals (tombstone = closed interval, never a DELETE) |
| `wg_fact` | historical facts: `team_role` (user in room), `agent_session`, `agent_grant`; closed, never deleted |
| `wg_layout` | diagram layout documents: shared default (`owner_user_id=''`) + per-user override; independent of graph data |
| `wg_cursor`, `wg_gap` | worker watermark; findings (repaired events, source holes, poison events, retention gaps) |

Folding rules (`work_graph_core`): ids and timestamps are functions of the source event only; each field is decided separately
from all assertions in rank order (a late `task_updated` cannot clobber an earlier `task_created`'s text); a tombstone or an
edge remove has its own rank, so an older late add cannot resurrect it; artifact versions are immutable (first writer's content
stands, only lifecycle `status` moves); scope narrows (narrowest scope among the entity's own statements wins), lists union.
Edge endpoints get `ensure` stubs from the same event, so no edge ever points at a missing node; a stub's scope applies only
while no real statement exists.

Durable task: a task is a graph node (`task:<room task id>`, alias `collab_task`) with `attrs.room_task_id` and
`room_task_ttl: 24h`; the HCN room task keeps its 24 h TTL as the execution handle, the graph node outlives it.

## Producer
`WorkGraphWorker` (started with the server when enabled): incremental by `(source_timestamp, event_id)` watermark with a
10-minute look-back, plus `WorkGraphReconciler.reconcile()` every ~5 minutes and on `POST /api/v1/graph/work/reconcile`
(operator). Reconcile (1) applies every source event the ledger lacks (dropped / late / outside the window), (2) derives
artifact and version nodes and their project/room/task/issue/mission links from the registry (the registry emits no events
yet), (3) enriches issue nodes from the catalog, (4) records sequence holes inside a source, (5) reports **orphans in both
directions** (`missing`, `unlinked`, `dangling`; `unbound` artifacts have nothing to link to and are listed, not hidden).
`rebuild()` = replay from clean; it refuses with `snapshot_required` (HTTP 409, `wg_gap.retention_gap`) when sources no longer
hold events the projection was built from.

## Routes (extend `/api/v1/graph/*`; HCN door: `/api/v1/collab/graph/work/*`)
`GET entities` (kind, project_id, status, as_of, include_tombstoned, limit<=200, cursor) - `GET entity?ref=` (ETag) -
`GET search?q=` - `GET snapshot?root=&depth<=4&max_nodes<=1000&edge_types=&as_of=` - `GET projects/{id}|tasks/{id}`,
`GET centric?ref=` (issue refs contain `/` and `#`, so `ref=`) - `GET facts?ref=&kind=&as_of=` - `GET layout?ref=&which=effective|default|mine`
(ETag) - `POST layout` `{ref, shared, document, if_match}` (also `If-Match` header; 428 without, 412 stale) -
`GET status`, `POST reconcile` (keyed operator only). `ref` is a urn, `kind:native` (e.g. `task:abc`, `issue:owner/repo#7`) or `system:alias`.
Keyed door: reads need the viewer role, POSTs the operator role. HCN door: signed-in non-guest members.

## ACL
One rule (`work_graph_core.visible`, mirrors `graph.can_see`), applied **inside every SQL statement** (so `LIMIT`, `has_more`,
degrees never see hidden rows) and again in Python: unauthenticated no; `org` any fleet key (never a signed-in person);
`room`/`mission` an operator or a member whose `graph_access` covers the room (mission: the (room, mission) pair); `private` only
named grantees; guests, agents and people without a non-guest membership get the one 404. Graph expansion joins BOTH endpoints
through the predicate (filter before traversal and before edge expansion). Responses never echo scope lists (room ids,
grantees), totals are not exposed, an invisible and a missing root are the same error. **Artifacts and versions are not decided by
room linkage**: their visibility is the artifact registry's own gate (`ArtifactGraphGate`, read grant + retraction), called per
row after the SQL prefilter. Layout GET removes positions of nodes the viewer cannot see; PUT strips them.

## Known limits
* Room-derived read access to artifacts is a P03 decision (grants written when an artifact is linked); until then a member sees
  an artifact only with an explicit registry grant.
* A project is visible to a member through the project's rooms (plus named grantees: its creator and project members); a project
  manager who is in no room does not see it in the graph.
* An alias is bound once (`wg_alias` PK); a conflicting second claimant is ignored.
* GitHub issues are enriched only when the graph already refers to them; mapping catalog projects to HCN projects is not done.
