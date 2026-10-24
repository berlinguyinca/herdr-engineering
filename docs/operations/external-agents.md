# External coworker / remote agents (T10)

Lets another person's Claude/Pi agent, running on their own machine, take part in ONE Herdr Collaboration Network (HCN) room
with a virtual tab in the room's Tabs panel. It extends the existing HCN service; there is no second service, store or
credential system. Federation between servers is a separate, later feature and is not implied here.

## What an external agent is
An ordinary HCN agent record with an `external` block: bound to one room, sponsored by the room owner who invited it, limited to
`read` and optionally `post`, with a credential that has a hard expiry. It is its own principal kind in every view
(`kind: "external"`, message sender type `external`, lineage actor kind `external`).
It never gets: a terminal, a shell, a pane or session link, `instruct`/`execute` (tasks arrive in T11), the member roster,
the flow, the task list, membership or grant events, workspace links, missions, or anything from before it was enrolled.

## Flow
1. The room owner opens Tabs > "Invite a remote agent" (or `POST /api/v1/collab/rooms/{room}/externals/invites`):
   `{label, actions: ["read"] | ["read","post"], lineage: bool, ttl_seconds (60-86400, default 3600),
   credential_ttl_seconds (300-604800, default 86400)}`. The reply holds the one-time `code` (`hcj_...`); only its digest is stored.
2. The coworker's bridge redeems it (public route, the code is the authentication; **outbound only**):
   `POST /api/v1/collab/agents/join {"token": "<code>", "harness_type": "claude-code"}` ->
   `{agent_id, credential, device_id, expires_at, room_id, actions, lineage, terminal: false}`.
   Use the credential as `Authorization: Agent <credential>` and send `X-Herdr-Device: <device_id>` on every call.
3. Presence: `POST /agents/external/heartbeat {"status": "idle|working|waiting|blocked|done|failed", "activity": "<=140 chars"}`
   at least every 60 s. No heartbeat for 90 s shows the agent as stale.
4. Chat (if invited): `GET /rooms/{room}/events`, `POST /rooms/{room}/messages` (also via the MCP/A2A endpoints).
5. Lineage (if invited with `lineage`): `POST /agents/external/lineage`, see `docs/contracts/lineage-events.md` ("External agents").
6. `POST /agent/rotate` replaces the secret (same expiry); `POST /agents/external/leave` withdraws.
7. Revoke: Tabs > the agent's tab > "Revoke access" (`POST /rooms/{room}/externals/{agent}/revoke`, any owner of the room) or the
   sponsor's `POST /agents/{agent}/revoke`. The next request with the credential is a 401, streams end, new messages, heartbeats
   and lineage are refused; what it already posted or reported stays visible and its tab stays, marked revoked.
   Open invites can be cancelled the same way (`.../externals/invites/{invite}/revoke`).

## Configuration
No environment variables and no new YAML keys: the limits are constants in
`herdr_engineering/control_plane/collab_external.py` (room cap 25, 10 invites per owner per hour, stale after 90 s, ...).
The feature is available wherever HCN is enabled; room owners are the only ones who can create invites. In the isolated
`--collab-only` process, enrollment, presence, chat and the roster work, but lineage answers 404 (that process has no graph).

## Rollout and rollback
Code-only; new events (`join_token_created` with an `external` block, `external_status_changed`) are additive and ignored by
older code, which would treat an enrolled external as an ordinary agent limited by its grant. Roll back by reverting the PR;
to cut access first, revoke the agents (or let the 24 h credentials lapse). Web-only rollback removes the invite form and the
virtual tab; the server roster stays.

## Known limitations
- Revocation is evaluated inside the ingest ledger lock, immediately before admission. An event admitted a moment before the
  revoke call returned is stored (it was admitted first); nothing admitted after is.
- Presence and the per-minute rate windows are in memory and reset on restart. The lifetime lineage counts (2000 per credential,
  5000 per sponsor, 5000 per room) are durable; after a restart a count can be too high by up to 24, never too low. The counts are
  on writes, so updating an existing object spends allowance the same as creating one.
- The client address used for failed-redemption counters and enrollment audit rows is the one the HTTP layer already resolves
  (the peer, or `X-Forwarded-For` only from a loopback/private proxy). Behind a proxy that is not trusted it is the proxy's address.
  The counters are in memory (aggregate, 256 addresses, then "other"); only successful enrollments write the address to the log.
- Lineage needs the combined server. In the production split (dashboard in front of an isolated `--collab-only` service) the
  lineage route answers 404; enrollment, presence, chat, the roster and revocation work there.
- An external agent can only link its own nodes (`spawn`); connecting it into local lineage (a handoff from a local agent) needs
  an operator key, because ordinary keys cannot see room-scoped nodes.
- A bridge reference client is not part of this change; the protocol above is what a bridge implements.
- The browser smoke test (`tests/playwright/external-agent-tab.test.mjs`) covers the virtual-tab renderer only. The invite
  form, the revoke button handler and the chat rendering of an `external` sender are exercised by unit tests and review, not in a
  real browser against a live server.
