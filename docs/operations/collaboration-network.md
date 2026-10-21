# Collaboration network (HCN) — operator guide

Status: P1 foundation, **off by default**. Spec package:
`docs/specs/herdr-collaboration-network-2026-10-21/`; reuse map in `audit.md`;
decisions in `docs/adr/ADR-0007-collaboration-network-foundation.md`.

## Enable with BinView Cognito (recommended)
Users sign in with the BinView user pool; only members of one Cognito group get in.
```bash
herdr-eng control-plane web --host 127.0.0.1 --port 18766 --pg-dsn "$DSN" \
  --collab-cognito-pool us-west-2_GjtcM0PCp \
  --collab-cognito-client 6mcn0idau4b5uhtkd1b2g6c9qg \
  --collab-cognito-group dev
```
* Install the extra: `pip install 'herdr-engineering[collab]'` (PyJWT + cryptography).
* Clients send the Cognito **access** token (`Authorization: Bearer …`). ID tokens,
  other app clients, other pools and users outside the group are 401. The user id is
  the token's `username`.
* Create the group once (it does not exist yet): `aws cognito-idp create-group
  --user-pool-id us-west-2_GjtcM0PCp --group-name dev --region us-west-2`, then add people.
* Group removal takes effect within ~60 s when the host has AWS credentials allowing
  `cognito-idp:AdminListGroupsForUser`; without them it waits for the token to expire
  (about an hour). Open streams end when the token expires.
* MFA is a pool setting and the pool currently has MFA **OFF**; enforcing it is a
  decision for the pool owners, not something this server can do.
* A person can be invited to a room once they have signed in to this server once
  (or when the lookup above is available).

## TLS on the tailnet
Keep the server on loopback and let Tailscale terminate TLS (real Let's Encrypt
certificate, tailnet-only, no Funnel):
```bash
deploy/control-plane/collab-tailscale-serve.sh up 18766 9447   # https://<host>.<tailnet>.ts.net:9447
deploy/control-plane/collab-tailscale-serve.sh down 9447
```
Pick a free HTTPS port: on bender 443 (another service) and IPv4 8443 are taken.
Because the server binds 127.0.0.1, `--collab-tls-terminated` is not needed. Verified
live on bender: valid certificate, plain HTTP refused, same-origin POST accepted,
cross-origin POST 403. Behind the proxy the rate limiter keys on `X-Forwarded-For`
(trusted only from a loopback peer).

## Enable (pilot, static tokens)
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
