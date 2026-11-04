# Team rooms: attach a team, the guest level, the Suggest queue

Slice 1 of `docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md`. Everything here is off until
`control_plane.collab.team_rooms.enabled` is `true`. With it absent or false nothing changes for anybody, and the new routes
answer 404 (except `detach`, below).

## What it does

1. **Attach a team to a room.** The room owner (a person, never an agent) attaches an explicit team (made with
   `herdr-eng teams create`) to one room and names which of their own agents hold which role. The agents join the room as
   ordinary room participants with the fixed actions of the role:

   | Role | Actions |
   |---|---|
   | `issue_writer` | read, post |
   | `implementer` | read, post, execute |
   | `tester` | read, post, execute |
   | `reviewer` | read, post |

   `coordinator` is refused (it is slice 3). Only the owner's own, active, non-external, non-helper agents can be attached, and
   an agent that already holds access to the room is refused with 409 (remove it first) rather than merged. The grants carry
   the team id, have no expiry, and end only with `detach`; the owner cannot edit them by hand (409) so a detach always removes
   exactly what the attach made. One team can sit in many rooms; a room holds at most 10 teams and a team at most 20 agents.
   **Attaching never makes the room's people team members**: the team's membership is its own data (the attached agents), and
   the Work Graph shows it as `team_role` facts on the team plus a live `references` edge team -> room. Attach and detach are
   separate events in the collab log.
2. **Guest level, per room.** `read_only` | `suggest` | `direct`. The default is `suggest` (configurable to `read_only`; `direct`
   is refused as a default). Only the owner user sets it. **Direct is stored and validated (agents or roles it may address, never
   "any"; per hour, open and text caps) but is not implemented: it behaves as `suggest`.** Slices 2 and 3 define what it may
   do; until then it fails closed. Nothing lets a guest assign work to an agent.
3. **The Suggest queue.** A guest (a room member whose role is `guest`) files a request. It is a `pending` row in the owner's
   queue; it creates no task and reaches no agent. At `read_only` the guest gets 403 and nothing is written. The owner approves
   or rejects; approving creates one **new** task made by the owner (`origin: suggestion`) whose text is the owner's command
   (the owner's `command`, or a fixed sentence) and whose request rides as `quoted_source`, tainted
   `external-source:guest`, in the target agent's `tasks_inbox`. Approval authorises the work; it does not clear the taint and
   it never turns the guest's words into the command.

   States: `pending` -> `approved` -> `materialized` (the task exists), or `rejected`, `withdrawn` (by the guest, before a
   decision), `expired` (derived at read after `suggestion_ttl_seconds`; no event is written, expired rows do not count toward
   the caps). Approve and reject need the digest of the request text the owner was shown (409 otherwise); a repeated decision is
   a replay and writes nothing; terminal states are terminal. A crash between the decision and the task is healed by repeating
   the approve: the same task is reused, never a second one.
4. **What a guest sees** (while the flag is on): the room's messages, artifacts shared into it, and their own requests (text,
   state, digest, dates; never the owner's reason, command, target or task). The task list and task events are hidden from guests
   (`list_tasks` is empty, the feed skips `task_created`/`task_updated`); the roster, people, flow and team attachment were
   already or are now hidden. With the flag off a guest sees tasks exactly as before (a test pins both).

## Turn it on

```yaml
control_plane:
  collab:
    team_rooms:
      enabled: true                 # absent = off
      default_guest_level: suggest  # suggest (default) | read_only; never direct
      suggestion_ttl_seconds: 604800
      max_pending_per_guest: 5
      max_pending_per_room: 50
      submits_per_10min: 10
      decisions_per_minute: 30
      kill_switch: false            # true turns it all off at once
```

The block is read on every request through the fleet config store; a missing, unreadable or invalid file turns the feature
OFF (like `insights`). Turning it on changes existing rooms' guests from read only to Suggest: they can file a request that
waits, and nothing executes. Set a room to `read_only` first if you do not want that.

## Operate it

```bash
export HERDR_COLLAB_TOKEN=...            # a collaboration bearer token for YOU (the owner); the name is yours to pick (--token-env)
herdr-eng rooms attach ROOM TEAM --role implementer=AGENT_ID --role reviewer=AGENT_ID
herdr-eng rooms teams ROOM
herdr-eng rooms guest-level ROOM [read_only|suggest|direct [--agents A,B --per-hour 6 --max-open 3]]
herdr-eng rooms queue ROOM [--state pending]
herdr-eng rooms approve ROOM SUGGESTION --agent AGENT_ID [--command TEXT] [--edited-text TEXT]
herdr-eng rooms reject ROOM SUGGESTION [--reason TEXT]
herdr-eng rooms detach ROOM TEAM
# a guest, with their own token:
herdr-eng rooms suggest ROOM "please look at the flaky build" [--key K]
herdr-eng rooms withdraw ROOM SUGGESTION
```

The same operations over HTTP (`/api/v1/collab`, a bearer token; every miss and every non-owner answers one 404):

| Route | Who |
|---|---|
| `POST /rooms/{room}/teams/{team}/attach` `{"roles": {"implementer": ["agent-id"]}}` | owner |
| `POST /rooms/{room}/teams/{team}/detach` | owner (reachable with the flag off) |
| `GET /rooms/{room}/teams` | owner |
| `GET` / `POST /rooms/{room}/guest-level` `{"level": ..., "direct_limits": ...}` | any member reads; owner sets |
| `POST /rooms/{room}/suggestions` `{"text": ..., "idempotency_key": ...}` | guest |
| `GET /rooms/{room}/suggestions[?state=]`, `GET .../suggestions/{id}` | owner: all; guest: own |
| `POST .../suggestions/{id}/decide` `{"decision": "approve|reject", "expected_digest": ..., "target_agent_id": ...}` | owner |
| `POST .../suggestions/{id}/withdraw` | the submitter |

The owner sees pending requests in the room attention inbox as category `suggestion_decide` (ids and dates only, never the
request's words) when `control_plane.collab.insights.enabled` is also on; the queue routes above work without it.

## Roll back

- Fast: `team_rooms.enabled: false` (or `kill_switch: true`). The new routes are 404 again, guests see tasks again, no new
  request or attachment can be made. Nothing is deleted. A queued request stays in the log and reappears if you turn it on.
- Remove the team's agents: `herdr-eng rooms detach ROOM TEAM` (works with the flag off; it is a safety event, so a full log
  cannot block it). `agents/{id}/remove` also works on a single attached agent.
- An image without this code ignores the new event types in the log. Detach before rolling the image back, because an older
  image would keep the team's grants (they are written as ordinary grants with a `team_id` marker).
- No migration, no table, no environment variable. State is collab-log events and a Work Graph projection.

## Choices made where the design left room

- `POST` (not `PUT`) for the guest level: the collab door reads bodies on POST only.
- Role routing is slice 2, so approval names `target_agent_id` (an agent that may execute in the room).
- Only `guest` members submit. Owner, contributor, viewer and every agent (including external ones) get 403; there is no MCP tool
  for suggestions, levels or attachments.
- A team id must look like `team-` plus 20 hex digits and, when the Work Graph is present, must be an active explicit team (404
  missing, 409 archived, 503 if the graph cannot answer). The collab-only process has no Work Graph and checks the shape only
  (`verified: false` in the response).
- Reject also needs the digest (same rule as approve). `suggestion_withdrawn` is a fourth suggestion event the design's state machine implies.

## Not built in this slice

Web UI (no Rooms, queue or approval screen; only the API, CLI and the attention category), the fleet-wide operator inbox
(`attention_inbox.py`), role routing and people as role holders (slice 2), coordinators (slice 3), assistant briefs and the
email path (slice 4), approval-gated external actions (slice 5), `member_visibility`, `my-tasks`, the Direct level's behaviour
and `role`/`team_id` on a suggestion.

---

# Slice 2: roles, routing by role, people as role holders, leases

Slice 2 of the same design (`TEAM_COLLABORATION.md` sections 4.2-4.5, 7, 8, 14, 15). It sits behind its own sibling flag
`control_plane.collab.team_roles.enabled` and ALSO needs `team_rooms` on (roles resolve through attachments). With either off,
`create_task` rejects the new fields as unknown (400, exactly as today), every new route answers 404 (except `roles clear`, which is a
revocation and stays reachable), the sweep does nothing, and the review rule is the old agent-only one. A test pins that.

## What it does

1. **Role holders, agents and people.** A team's roles are `issue_writer` (kind planner), `implementer`, `tester` (kind implementer)
   and `reviewer` (kind reviewer); `coordinator` is refused (slice 3). They are kept **per room and team**: an agent holds a role by
   being attached (slice 1, unchanged), a person holds it by `team_role_assigned`, written by the room **owner user** only (an agent
   gets 403 and an `audit_denied`, anyone else the usual 404). The person must be a current member of the room (an external guest
   with a sign-in qualifies). Assigning a role never changes the person's room role and a member is never implicitly a holder. A
   detach takes the team's people out of their roles. At most 20 people in roles per room and team.
2. **Routing by role.** `POST /rooms/{room}/tasks` takes `target_role` (and `team_id` when more than one attached team holds the role:
   400 `team_required` otherwise) instead of `target_agent_id`. The rule, in order: take the holders that can take a task now (a person
   who is still a member, an agent that is active and holds `execute` in the room), drop any that are not independent when the task
   is a review, drop full ones (agents `MAX_OPEN_TASKS_PER_AGENT`, people 20), pick the **fewest open tasks**, break a tie by the
   **smallest principal key** (`agent:<id>` sorts before `user:<id>`). The holder is written into the `task_created` event as
   `routed_to`, so a replay or restart never re-routes; the same idempotency key returns the same task, also after a restart. **Nobody
   eligible is a 409 `no_role_holder` and nothing is written**: it never falls through to an agent outside the role. A `reviewer` task
   must name `reviews_task_id`.
3. **Tasks for a person.** `target_user_id` names a room member directly; a person holder is reached through the role. Such a task has
   no agent target (it is in no `tasks_inbox`) and lands in the person's queue: `GET /rooms/{room}/my-tasks` (any non-agent member,
   guests included; their own tasks only, text marked untrusted) and, for members the attention inbox already serves, the categories
   `task_assigned` and `task_overdue` (ids, role and dates only, never the words; needs `insights.enabled`). They answer with
   `POST /rooms/{room}/tasks/{id}/respond {"status": "accepted"|"done"|"declined", "result": ...}`; only the assignee can.
4. **The clock.** A person never polls, so a server-side sweep (a daemon thread of the control plane every `sweep_interval_seconds`,
   default 30) evaluates every open person task from the replayed state and `now`. Per task: a lease (`person_lease_seconds`,
   default 3 days, ceiling 14 days; the room owner may set `lease_seconds` per task) with reminders at 50% and 90% (`task_reminded`,
   each once, before expiry only), then the **no-response policy**, one per team (`herdr-eng rooms role-policy`, default `escalate`,
   the task may carry its own if the owner creates it): `reassign` (move the role to another eligible holder **once**, new lease, then
   escalate; a direct assignment has no role so it escalates), `escalate` (`task_escalated`: status `blocked`, a `task_overdue` row in
   the owner's attention; the owner cancels or re-issues it), `wait` (`task_lease_extended` by another lease, at most
   `max_lease_extensions` times and never past the hard expiry, then escalate). The hard expiry (`hard_expiry_seconds`, default 30
   days) replaces the 24 hour `TASK_TTL` for these tasks. Accepting ends the lease. A holder who left the room or lost the role while
   holding an open task goes into the same policy at the next sweep, without waiting for the lease.
   Crash safety: no in-memory deadline and no table. The deadline, the reminders sent, the extension count and the policy are in the
   `task_created` / `task_reminded` / `task_lease_extended` / `task_reassigned` / `task_escalated` events and rebuilt on replay; every
   outcome changes the state the next sweep reads, so a repeated or concurrent sweep commits nothing. The sweep skips while the
   service is degraded and stops quietly on a full or unavailable log.
5. **Independent review across agents and people.** With the flag on a review (`reviews_task_id`, to an agent, a person or a role) is
   refused when the reviewer is the implementer, anyone who held the work before a reassignment, the creator of the work, a creator
   anywhere up its parent chain, or anyone in the same configured `reviewer_groups` as one of those. Principals are compared as
   `agent:<id>` / `user:<id>` keys. Agents owned by the same person still count as different reviewers, as before; a person and their
   own agents are different principals too unless you put them in a group.
6. **What an external person sees.** Their own assigned tasks (`my-tasks`) and the artifacts shared into the room. Not the roster,
   agents, flow, team attachment or roles, other people's tasks, or task events: slice 1 already hid the task list and task events from
   guests, and a person's task is hidden from everyone but the assignee, the room owner and the task's creator in the list, the feed
   and the state view. `member_visibility` (owner, per room, default off, `herdr-eng rooms member-visibility ROOM on`) gives members
   back exactly the room's task list and task events of agent tasks; person tasks of others, the roster, agents and flow stay hidden.

## Turn it on

```yaml
control_plane:
  collab:
    team_rooms: {enabled: true}          # required
    team_roles:
      enabled: true                      # absent = off
      person_lease_seconds: 259200       # 60 .. 1209600
      hard_expiry_seconds: 2592000
      max_lease_extensions: 3
      reminder_fractions: [0.5, 0.9]
      default_no_response_policy: escalate   # reassign | escalate | wait
      reviewer_groups: []                # e.g. [["user:alice", "agent:AGENT_ID"]] counts as one reviewer
      sweep_interval_seconds: 30
      kill_switch: false
```

Read per request through the fleet config store (a missing, unreadable or invalid file turns it OFF); the sweep thread re-reads the
interval each pass. No migration, no table, no environment variable.

## Operate it

```bash
herdr-eng rooms assign-role ROOM TEAM tester dave        # a person (user:ID or just the ID); agents join a role by attach
herdr-eng rooms clear-role ROOM TEAM tester dave
herdr-eng rooms roles ROOM TEAM                          # holders, kinds, who can take tasks, the policy
herdr-eng rooms role-policy ROOM TEAM [reassign|escalate|wait]
herdr-eng rooms assign ROOM "verify the fix" --role tester [--team TEAM] [--lease 3600 --policy wait] [--reviews TASK]
herdr-eng rooms assign ROOM "look at this" --user bob
herdr-eng rooms member-visibility ROOM [on|off]
herdr-eng rooms my-tasks ROOM                             # any member, with their own token
herdr-eng rooms respond ROOM TASK accepted|done|declined [--result TEXT]
```

| Route (`/api/v1/collab`) | Who |
|---|---|
| `POST /rooms/{room}/teams/{team}/roles` `{"role", "holder": "user:ID"}`, `.../roles/clear`, `.../policy` `{"no_response_policy"}` | owner |
| `GET /rooms/{room}/teams/{team}/roles` | owner |
| `GET` / `POST /rooms/{room}/member-visibility` `{"enabled": bool}` | any member reads; owner sets |
| `POST /rooms/{room}/tasks` `{"target_role" \| "target_user_id", "team_id", "lease_seconds", "no_response_policy", ...}` | as for any task (`task` right; an agent only by role and with an accepted parent) |
| `GET /rooms/{room}/my-tasks`, `POST /rooms/{room}/tasks/{id}/respond` | the assignee (a person, any room role) |

## Roll back

- Fast: `team_roles.enabled: false` (or `kill_switch: true`). New fields are unknown again, the routes are 404, the sweep stops.
  Nothing is deleted. Person tasks already created stay in the log: they are still visible only to the assignee, owner and creator, and
  nobody chases them (no reminder, no escalation) while the flag is off. Cancel the ones you no longer want (`POST .../tasks/{id}/cancel`).
  A task is also only as good as its requester's current authority: once whoever asked is removed from the room (or loses the `task`
  right), the assignee no longer sees it, cannot answer it (404), and the sweep escalates it (`creator_gone`) instead of re-routing it.
- `roles clear` works with the flag off. `detach` also takes the team's people out of their roles.
- **Do not roll the image back while the log holds a task for a person newer than about 8 days, whatever its status.** An image
  without this code replays such a task with no agent target, lists its text to everyone who can read the room's task list, and
  `room_state` and `room_flow` fail with a `TypeError` for every owner and contributor of that room (checked by replaying a log that
  holds one such task into the code of `origin/main`). Cancelling does not help, because finished tasks stay in memory until the old
  image prunes them (a task expires after 24 hours, then is dropped after 7 days). Roll back the FLAG, not the image.
- No migration, no table, no environment variable.

## Choices made where the design left room

- Role holders are per (room, team), not per team: authority is decided against a room, an external person only exists as a room
  member, and the slice-1 attach already binds agents that way. The route is `.../rooms/{room}/teams/{team}/roles`, not the sketch's
  `/teams/{team}/roles`.
- Agents become holders only by attach (their grants and actions are fixed there). `assign-role` takes people only.
- Agents whose role lacks `execute` (`issue_writer`, `reviewer`) cannot be routed a task (`create_task` and `tasks_inbox` need
  `execute`); the request fails closed (409) rather than widening their rights. A person can hold those roles.
- A person task is a task with `target_agent_id: null` and `target_user_id`; no new Work Graph entity kind (design 6.2 left it open).
  The projection carries the task with no words and no assignee.
- The hard expiry is its own field, not `bounds.expires_at`, because `bounds` marks a bounded delegation all over `collab.py`.
- A decline is terminal; it is not re-routed. The no-response policy is for silence.
- Only the room owner sets a per-task lease or policy.

## Not built in this slice

Web UI (no role, my-tasks or overdue screen), a way for the owner to re-issue a blocked task in one step (cancel and create again),
re-routing on a decline, `task_assigned` for guests in the attention inbox (a guest reads `my-tasks`; the attention inbox stays 404 for
guests), email or push notification of a reminder (the reminder is an event and the queue entry), the fleet-wide `attention_inbox.py`,
coordinators (slice 3), assistant briefs (slice 4), approval-gated external actions (slice 5), Direct behaviour.

# Slice 3: coordinators with limits

Slice 3 of the same design (`TEAM_COLLABORATION.md` section 10, 10.2, 16 and the T-TC threat rows). A coordinator decomposes an
operator-approved root task into tasks for a team's role holders and tracks them. Two flags, both under
`control_plane.collab.coordinators`, both off by default:

- `enabled` (also needs `team_rooms` and `team_roles`): operator-owned coordinators, an agent the operator owns.
- `external.enabled` (also needs `enabled`): an external person, opted in per team by the operator.

With either off, `create_task` behaves exactly as before (a test pins that), every new route answers 404, the sweep does nothing
and a coordinator's authority is gone. The OFF direction of every revocation stays reachable.

## What it does

- **Operator-owned coordinator.** The room owner designates one of their own active agents (not a foreign agent, not a helper, not
  one that already holds access in the room) as the coordinator of a team in a room. It gets `read`, `post`, `instruct` and `execute`
  on the room (`execute` because it must accept the root task; design table 7 lists read/post/instruct). It can create tasks only
  by `target_role`, only for roles the team has, only for that team, and only under a root task the operator made and the coordinator
  accepted (an agent cannot create a root task). Cap: `tasks_per_hour` (default 20) and `max_open` (default 20), counted from the
  replayed log so a restart does not reset them.
- **External coordinator.** The owner turns on `external-coordination` for a team (with the roles the person may assign to and
  optional lower limits), then designates a current guest of the room. They act as themselves (events carry their user id and
  `origin: coordinator`), with a new per-team `coordinate` right, not the contributor or guest right. Caps default to 5 per hour,
  are always strictly lower than the operator-owned cap and can be lowered further per team, never raised past the service-wide
  ceiling. They see status only: no results, no task list, no task by id, no assistants, no brief references.
- **Cannot, for either kind.** Approve any external action, grant a role or permission (to anyone, itself included), change the
  guest level, change the coordinator designation or the opt-in, attach or detach a team. Each attempt is 403 with an audit event
  under the coordinator's own identity (a designated external person gets 403 where a plain guest gets the old 404, so the refusal is
  recorded).
- **No assistant loop.** A task created by an agent is refused (400 `coordination_loop`) when its target is anywhere in the chain
  of creators above it, or when it targets an assistant after an assistant already appears in the chain. This covers the
  third-agent case (A to B to C to A). An assistant is derived from the log (designated coordinator agents, and agents claimed as an
  assistant runtime), never from `harness_type`.
- **Independent review.** A coordinator is never the independent reviewer of work it decomposed, and neither is anyone above it in
  the chain. This holds after a revocation, because the requeue keeps who the work came from.
- **Revocation.** Clearing the designation, turning the opt-in off, removing the person from the room, detaching the team, revoking
  the agent, or the identity provider dropping the person's group (the sweep notices it by itself, within the 60 second recheck
  window): an operator-owned coordinator's open tasks are cancelled; an external person's open tasks go back to the role queue under
  the operator with `attempt` raised by one, so the old run's update is a 409 `stale attempt`. If nobody is eligible to take a
  requeued task it is cancelled. Repeating a revocation changes nothing.

## Turn it on

```yaml
control_plane:
  collab:
    team_rooms: {enabled: true}
    team_roles: {enabled: true}
    coordinators:
      enabled: true                  # absent = off
      kill_switch: false
      tasks_per_hour: 20             # 2 .. 200
      max_open: 20                   # 1 .. 100
      external:
        enabled: false               # second flag; absent = off
        tasks_per_hour: 5            # 1 .. coordinators.tasks_per_hour - 1
        max_open: 5                  # <= coordinators.max_open
        max_depth: 2                 # 1 .. 3
        max_items_per_request: 10    # 1 .. 50
```

Read per request (a missing, unreadable or invalid file turns it OFF). No migration, no table, no environment variable.

## Operate it

```bash
herdr-eng rooms coordinator ROOM TEAM [agent:ID|user:ID]      # show, or designate (owner only)
herdr-eng rooms coordinator-clear ROOM TEAM
herdr-eng rooms external-coordination ROOM TEAM [on --roles implementer,tester --per-hour 3 | off]
herdr-eng rooms coordination ROOM                              # a coordinator's own tracking
```

| Route (`/api/v1/collab`) | Who |
|---|---|
| `GET` / `POST /rooms/{room}/teams/{team}/coordinator` `{"holder": "agent:ID" \| "user:ID"}`, `.../coordinator/clear` | owner |
| `POST /rooms/{room}/teams/{team}/external-coordination` `{"enabled", "limits": {"roles", ...}}` | owner |
| `GET /rooms/{room}/coordination` | the coordinator (status only for an external person) |
| `POST /rooms/{room}/tasks` with `target_role`, `parent_task_id` | the coordinator, within its limits |

## Roll back

- **Fast and safe: the flag.** `external-coordination off` (per team) revokes one external coordinator at once and requeues their
  work. `coordinators.external.enabled: false` or `coordinators.enabled: false` (or `kill_switch: true`) withdraws the authority
  everywhere: new tasks are 403, the routes are 404, tasks a coordinator made stop being served, and the next sweep with the flag
  back on requeues or cancels them. Turn the opt-ins off per team first if you want the requeue to happen while you are flagged off.
  Nothing is deleted from the log.
- **Rolling the image back is safe for reading, not for state, and I would still do the flag first.** Checked by replaying a log that
  holds a designation, an opt-in, coordinator child tasks, a revoked external person and a `task_requeued` event into the code of
  `origin/main` (the slice 2 code is the same on these paths): the log replays cleanly (not degraded), and `room_state`, `room_flow`
  and `list_tasks` answer for owner, contributor and viewer (a guest gets the usual 403 on `room_flow`). The old code ignores the
  new events: it grants the coordinator agent nothing, so tasks a coordinator created fail the "requester still has authority" test
  and no agent inbox serves them (checked: 0 tasks served). It also ignores `task_requeued`, so a requeued task shows the old
  `attempt` and the old requester, and the stale-run 409 fence does not exist on the old image. Nothing runs wrongly; revoked work
  just stays stuck until you cancel it. The slice 2 warning still applies if the log holds a task for a person: the root task of an
  external coordinator is one (R-TR14). So: turn the opt-ins off first, then the flags, and cancel what you do not want, before any
  image rollback.
- No migration, no table, no environment variable.

## Choices made where the design left room

- A coordinator is per (room, team), not per team, and the routes are room-scoped like slice 2 (`POST`, not `PUT`).
- The coordinator agent gets `execute` as well as `instruct` so that it can accept its root task.
- Coordinators assign by `target_role` only; they cannot name an agent or a person.
- An external coordinator must be a current guest of the room. A contributor already holds `grant_agent`, so the design's
  "no new right" rule excludes them, and the owner cannot be one.
- The external coordinator's root is a person task made by the operator for them (`target_user_id`); the Suggest queue only targets agents.
- Turning the opt-in off also clears the designation, so a later opt-in starts without a coordinator.
- On a requeue the operator adopts the task: it is attributed to the operator, the coordinator key is kept for review disqualification.
- A designated external coordinator gets 403 plus an audit event on owner-only routes where everyone else gets the old 404.
- Loop guard: chain-based, applied to every agent-created task while the flag is on.
- An external coordinator reading a task by id gets 404 while the flag is on (guests are blinded to tasks).

## Not built in this slice

Web UI of any kind (no coordinator or coordination screen), assistant briefs and the brief-reference check (slice 4: the refusal
"an external coordinator cannot read a brief reference" holds because no such route exists yet, and the only assistant API an
external credential reaches is rejected with 401, tested), approval-gated external actions (slice 5), and `direct` guest behaviour.
Coordinators cannot yet be handed work by Suggest requests.
