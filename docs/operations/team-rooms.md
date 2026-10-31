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
