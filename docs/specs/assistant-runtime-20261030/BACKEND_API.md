# Pi assistant activation API v1

This API converts one private immutable Pi package definition into one persistent
room agent. The control plane never accepts a command, argv, path, mount, environment
variable, secret, credential or requested action list. The activation request itself
authorizes the fixed room actions `read` and `execute`; package capabilities remain
non-authoritative requests.

## Owner routes

All owner routes derive identity from the bearer credential and require the current
room-owner role.

- `GET /rooms/{room}/assistant-hosts` lists bounded public summaries of operator-attested
  hosts. `ready` means the attestation is fresh, unused and enabled; it is not evidence
  that an agent was launched.
- `POST /rooms/{room}/assistant-runtimes` accepts exactly `package_id`, `host_id`, `name`,
  `idempotency_key`, and optional `selected_model`. The first accepted request returns
  HTTP 202. An exact replay returns HTTP 200 with `replayed: true`; changed use of the
  key returns HTTP 409.
- `GET /rooms/{room}/assistant-runtimes` and the targeted form return private lifecycle
  records. No claim token or agent credential is returned.
- `POST /rooms/{room}/assistant-runtimes/{runtime}/cancel` accepts an empty object.
  A queued request is cancelled immediately because no process exists. A claimed,
  starting or ready agent loses its room grant immediately, while its runtime remains
  in its current state with `cancel_requested: true` until the runner confirms process
  termination.

Public states are `queued`, `claimed`, `starting`, `ready`, `failed` and `cancelled`.
Only a matching runner `ready` receipt adds `launch_attestation`; queued/claimed/starting
must not be presented as evidence of execution.

## Operator runner routes

These routes exist only on the existing private operator listener. Public bearer or
agent credentials cannot call them.

- `POST /fleet/assistant-runtime-hosts` records a strict, maximum-five-minute attestation.
  The exact document contains the host, `task-restricted-v1` profile and digest, fixed Pi
  entrypoint/artifact digest, all five required enforcement booleans, exact resource
  limits, approved model IDs, issued/expiry times and a one-use nonce.
- `GET /fleet/assistant-runtimes` returns the claimable queue.
- `POST /fleet/assistant-runtimes/{id}/claim` accepts exactly `host_id` and
  `attestation_nonce`. It rechecks current owner, room, package, digest, profile, limits,
  host policy and model policy, atomically creates the persistent agent and fixed
  `read`/`execute` grant, and returns its credential plus claim token once. Only hashes
  persist.
- `POST .../{id}/heartbeat` carries the claim token, attestation nonce and a monotonically
  increasing bounded checkpoint. It extends both the 120-second process lease and the
  agent's room-grant deadline. Room authorization therefore expires without depending
  on a dashboard read or cleanup sweep.
- `POST .../{id}/report` records `starting`, `ready`, `failed` or `cancelled`. `ready`
  requires an instance ID and launch attestation exactly matching the claimed host
  snapshot. `cancelled` requires a prior owner cancellation request.
- `POST .../{id}/cancel-status` lets the claiming runner poll for cancellation using the
  claim token without placing that token in a URL.

## Recovery and privacy

Host and lifecycle records are event-sourced and survive restart. Private events never
enter room feeds, SSE/MCP reads or room change cursors. Audit contains room/action metadata,
not package digests, attestation nonces, checkpoints, claim tokens or credentials.

A stale host attestation fails a queued activation. A missed heartbeat fails the runtime
and revokes its agent. Active heartbeats and ready receipts recheck current owner,
package, model and host policy. Cancellation removes room execution immediately; open
tasks become failed only after a terminal runner receipt or lease expiry. Claims are
exactly once: if the one-time credential response is
lost, the runner must not start a second process and the owner must request a new activation.
Actual collaboration tasks use the minted agent credential and the existing task API;
the activation API does not introduce another task scheduler.

Docker and Apptainer definitions remain unlaunchable in v1.
