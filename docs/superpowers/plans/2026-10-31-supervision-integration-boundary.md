# Integration boundary for root

Branch: `feat/tab-supervision`. Base is canonical main `fabc61c` plus accepted
policy checkpoint `2b98781`. No root WIP has been merged or cherry-picked.
Root's actual AssistantService work is independently based in
`.worktrees/herdr-assistant-delegation` (`daca260` at coordination time);
`.worktrees/herdr-assistant-cli` is a separate core archive. Do not copy either
runtime implementation into this lane.

## Shared files

- `control_plane/collab.py`: optional `supervision_provider`, default policy-only
  provider; private `supervision_recorded` envelope in the existing collab journal;
  authoritative replay; current exact collaboration identity/grant/model checks;
  room projection and owner-only immutable grouping. No launch/run/stop path.
- `control_plane/api.py`: public collaboration static allowlist adds only
  `supervision-tree.js`; normal dashboard serves the shared catalog generically.
  No authentication/header policy changes or AssistantService constructor hooks.
- `/api/v1/assistant-delegations` remains root-owned and untouched.

## Authorized API

`GET /api/v1/collab/rooms/{room}/supervision` uses the same current principal and
room read authorization as Flow. Guests and outsiders cannot read it. The same
projection is included as `flow.supervision` in the existing room state long-poll.
Shared viewers receive roles, status, budgets, progress and task source IDs/status;
only each agent's owner receives task summaries, evidence references and recorded
runtime bindings. A native observation can enrich a recorded binding only after
an exact runtime-instance match. Nothing infers ancestry from native parent IDs.

`POST /api/v1/collab/rooms/{room}/supervision/link` accepts exactly
`{"job_id":"existing-job","workspace_id":"explicit-workspace"}`. Only the current
room owner can link an already-owned job, and every registered job agent needs a
current room grant. The link is immutable; one room maps to one workspace. This
endpoint never registers an agent, grants scopes, or launches a process.

## Trusted operator registration contract

The root adapter must first register/authenticate a collaboration agent and resolve
its existing accepted task/grants/models. Then enter:

```python
with collab.supervision_transaction() as supervision:
    # actor is constructed by the trusted operator from authenticated current
    # grants, never from model/HTTP flags. agent_id is the exact collab identity.
    supervision.create_job(actor, job_id, tab_id, agent_id, task_id, policy)
```

Use the same transaction for delegate/bind/checkpoint/charge/conclude/cancel.
Embedded sink writes fail closed unless the outer serialized service lock is held,
preventing module/service lock inversion. Only one authoritative process may write
this stream. The existing required sink persists the private outer envelope before
projection mutation. Replay is complete/contiguous and occurs before serving.

In this embedded collaboration path, `Policy.scopes` are exact collaboration action
IDs; capability-specific assistant grants stay with AssistantService and require an
explicit resolver in root integration. Family/model keys are `family/model-id` and
must remain within the room's current helper profile families/models; an unrestricted
model list still requires a trusted catalogue ID. Ancestor policies are checked on
live writes and verified outcomes; revoked grants/models cannot be bypassed with a
stale Actor. Accounting, cooperative stops and cancelled cleanup remain possible
through the trusted operator after revocation. Package registration grants nothing.

## Runtime work deliberately reserved to root

Automatic job/child registration before real pane spawn, exact native receipt
binding, approved Pi sandbox/resource attestation, runtime leases, user rolling
10M/12M budget enforcement, cooperative stop delivery, attested termination and
release/adoption/migration contracts remain root-owned. This UI never calls them.
Historical identity reservations are not released merely because a pane closes.

Legacy p5 has unknown original spawner. Root p2 is confirmed spawner of p3, p6 and
p7. Monitoring p5 does not change its original parent; adoption requires a separate
explicit human/operator event. No data migration/backfill is performed here.
