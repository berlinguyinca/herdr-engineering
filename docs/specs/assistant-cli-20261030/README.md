# `herdr-eng assistant` initial contract

This directory specifies the initial authenticated CLI over Herdr's existing
collaboration API. It is a thin client: identity and scope come from the server-side
credential, and the CLI provides no user-id, owner-id, or `--approved` override.

## Configuration

Set an HTTPS collaboration origin and a user or agent credential:

```sh
export HERDR_ASSISTANT_URL=https://herdr.example
export HERDR_ASSISTANT_AUTH_KIND=user
export HERDR_ASSISTANT_TOKEN_FILE="$HOME/.config/herdr/assistant.token"
```

The token file must be a regular, owner-owned file with no group or other permissions.
`HERDR_ASSISTANT_TOKEN` is supported for ephemeral injected environments, but the file
avoids exposing credentials in process arguments. Agent credentials additionally require
`HERDR_ASSISTANT_AUTH_KIND=agent` and `HERDR_ASSISTANT_DEVICE`. Plain HTTP is accepted only
on loopback. Redirects are refused so authorization headers cannot cross origins.

## Commands

```sh
herdr-eng assistant agents list --json
herdr-eng assistant agents list --room ROOM_ID --json
herdr-eng assistant agents inspect AGENT_ID --room ROOM_ID --json
herdr-eng assistant capabilities list --room ROOM_ID --json
herdr-eng assistant tasks list --room ROOM_ID --json
herdr-eng assistant tasks inspect TASK_ID --room ROOM_ID --json
herdr-eng assistant tasks inbox --json
herdr-eng assistant tasks create --file task.json --json
herdr-eng assistant tasks update TASK_ID --status accepted --json
herdr-eng assistant tasks update TASK_ID --status done --result 'Draft ready' --json
herdr-eng assistant approvals request --room ROOM_ID --file proposal.json --json
herdr-eng assistant approvals list --room ROOM_ID --json
```

`task.json` contains exactly:

```json
{
  "room_id": "room_123",
  "target_agent_id": "agent_123",
  "text": "Prepare the venue comparison",
  "idempotency_key": "venue-comparison-20261030"
}
```

Agent delegation may additionally include `parent_task_id`, which the server validates
against a task that agent accepted. Every mutation is sent once. Callers must reuse the
same idempotency key after an uncertain task-create result; the CLI never retries a
mutation automatically.

An approval proposal file contains `task_id`, `kind`, `summary`, `details`, and
`idempotency_key`. A proposal is only a pending request. There is intentionally no CLI
operation that converts one into authority; the authenticated grant/decision owner must
be implemented separately.

## Output

Successful and operational-error output is a versioned JSON object:

```json
{"schema_version": 1, "ok": true, "result": {}}
```

Peer task text and agent results remain untrusted data even though they appear inside a
successful envelope.

## Current boundaries

- `agents list` without `--room` is user-only. Agent credentials discover peers with a
  room-scoped command, where the server returns only visible agent summaries.
- Capability output describes current collaboration actions (`read`, `execute`, and
  `instruct`) and implemented task contracts. It explicitly reports that a general role
  or shopping-capability registry is unavailable.
- `tasks list` returns the room's newest 100 tasks. `tasks inspect` uses the targeted,
  room-authorized task endpoint, so older tasks remain addressable without overstating
  a bounded list result.
- Task inbox/update require an agent credential. Agent administration and grant approval
  remain server-owned and are not exposed by this slice.
- This CLI does not launch Pi, containers, browsers, email, purchases, or external writes.
