# Collaboration network (HCN) — operator guide

Status: P1 foundation, **off by default**. Spec package:
`docs/specs/herdr-collaboration-network-2026-10-21/`; reuse map in `audit.md`;
decisions in `docs/adr/ADR-0007-collaboration-network-foundation.md`.

## Enable (pilot, two users)
```bash
# user_id -> sha256(token). Generate a token and hash it (give the user the token,
# store only the digest):  T=$(openssl rand -hex 24); printf %s "$T" | sha256sum
echo '{"alice":"<sha256 hex>","bob":"<sha256 hex>"}' > collab-users.json
herdr-eng control-plane web --host 127.0.0.1 --collab-users-file collab-users.json \
    --pg-dsn "$DSN"        # durable log; without a DSN state dies on restart
```
Non-loopback binds additionally require `--collab-tls-terminated` (bearer
credentials need TLS in front). Without `--collab-users-file` every
`/api/v1/collab/*` route is 404 and `collab_v1` is absent from `/api/v1/health`.

## Model
* Humans authenticate with `Authorization: Bearer <token>`. Legacy `X-API-Key`
  roles and open (loopback) mode grant **nothing** on collab routes.
* A user enrolls an agent: `POST /agents/enrollment/start` (one-time code, 10 min),
  then the bridge calls `POST /agents/enrollment/complete` with the code and its
  `device_id`, receiving a credential (12 h, shown once; only its sha256 is stored).
  Agents authenticate with `Authorization: Agent <credential>` plus
  `X-Herdr-Device`. `POST /agent/rotate` replaces the credential.
* Room roles: owner, contributor, viewer, guest. Guests are read-only. An agent
  must be granted into a room by its owner (`read`/`post` only); its effective
  rights are `grant ∩ owner's role ∩ {read, post}` and vanish when the owner is
  removed, the agent is revoked, or any expiry passes.
* Non-members get 404 for every room route (no enumeration). Members only see
  events from their join point onward.
* All remote text is untrusted data: stored (secret-redacted), returned with
  `trust: "untrusted"`, never executed and never delivered to a terminal.
* `GET /audit?project_id=` (project managers) lists allowed/denied actions and
  never message text.

Routes (all under `/api/v1/collab`): `projects`, `projects/{p}/rooms`,
`projects/{p}/members`, `rooms`, `rooms/{r}`, `rooms/{r}/members`,
`rooms/{r}/members/{u}/remove`, `rooms/{r}/agents`, `rooms/{r}/messages`
(idempotency_key required), `rooms/{r}/events?cursor=&limit=`,
`rooms/{r}/events/stream` (SSE; re-authorized every tick, closes with
`{"type":"closed","reason":"access_revoked"}`), `agents/enrollment/{start,complete}`,
`agents/{id}/revoke`, `agent/rotate`, `audit`.

Revoking a human today means editing the users file and restarting (their
agents then fail the owner check immediately). Repeated denials from one
principal are persisted at most once per minute; the rest are counted.

## Known limits (release blockers for real users)
SSO/MFA not wired (static hashed tokens); no TLS handling in-process; no
Prometheus export yet (`CollabService.stats` counters only); artifacts, tasks,
A2A, MCP and UI are P2–P4. See `audit.md` for the blocker list.
