# Task approval proposal API v1

An approval proposal records that an agent or its user needs a human decision.
It is deliberately not an authority grant. This API has no approve, consume,
execute, send, purchase, or payment operation.

## Routes

`POST /api/v1/collab/rooms/{room_id}/approval-proposals`

The authenticated principal supplies exactly:

```json
{
  "task_id": "grant_...",
  "kind": "email.send",
  "summary": "Send the approved venue inquiry",
  "details": {"recipient_count": 2, "followups": 1},
  "idempotency_key": "proposal-1"
}
```

`kind` is an identifier of at most 64 characters. `summary` is 1–500
characters. `details` must be a JSON object whose canonical UTF-8 encoding is
at most 8 KiB. The idempotency key is required. Unknown fields, including
fields that purport to grant or approve authority, are rejected.

The task must be active in the route's room. An agent must be its assignee and
retain the room's `execute` permission. A user must own the assigned agent and
be the room owner, the task-chain starter, or the task creator's owner.
Credentials determine every identity; request bodies cannot override scope.

The first request returns HTTP 201. Repeating the same request and key returns
HTTP 200 with `replayed: true`. Reusing the key for different content returns
HTTP 409. Replay rechecks current membership, task relation, and agent grants.
Task idempotency is durable for tasks created after this contract shipped;
older task events did not persist idempotency metadata and cannot recover that
deduplication state after a restart.

`GET /api/v1/collab/rooms/{room_id}/approval-proposals`

Returns pending proposals owned by the authenticated user. A room owner cannot
see another user's proposal details merely because both users share a room.
An agent sees only proposals that it requested. Current room membership and
agent grants are rechecked. Proposal events are excluded from the shared room
feed; audit output contains action metadata rather than proposal details.

## Limits and lifecycle

Creation is rate limited to 20 proposals per requester per ten minutes and to
100 pending records per user per room. All records remain `pending`; this slice
does not implement a decision lifecycle. A future authority service must define
expiration, review, revocation, and consumption without treating these records
as executable grants.
