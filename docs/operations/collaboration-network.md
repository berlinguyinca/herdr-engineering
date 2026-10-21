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
  --collab-cognito-group local.dev
```
* Install the extra: `pip install 'herdr-engineering[collab]'` (PyJWT + cryptography).
* Clients send the Cognito **access** token (`Authorization: Bearer …`). ID tokens,
  other app clients, other pools and users outside the group are 401. The user id is
  the token's `username`.
* The group `local.dev` exists (created 2026-10-21; to recreate: `aws cognito-idp create-group
  --user-pool-id us-west-2_GjtcM0PCp --group-name local.dev --region us-west-2`). Add people with `aws cognito-idp admin-add-user-to-group`.
* Group removal takes effect within ~60 s when the host has AWS credentials allowing
  `cognito-idp:AdminListGroupsForUser`; without them it waits for the token to expire
  (about an hour). Open streams end when the token expires.
* MFA is a pool setting and the pool currently has MFA **OFF**; enforcing it is a
  decision for the pool owners, not something this server can do.
* A person can be invited to a room once they have signed in to this server once
  (or when the lookup above is available).

## Web page (invite people from the browser)
Open `https://<host>:9447/#/collab`, sign in with your BinView account (Cognito Hosted
UI, authorization code + PKCE; the password never reaches this server or page), then
create rooms, chat, and **invite by email**. The page links your verified email to your
account at sign-in, so invitations work by email: if the person has signed in before they
are added at once, otherwise the invitation is pending (7 days) and activates on their
first sign-in. Either way the owner sees the same reply. The invitee must still be in
the `local.dev` group. Browser sign-in uses its own app client, `herdr-collab-web`
(public client, code flow only, one callback URL). Tokens live in sessionStorage only.

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

## Public hostname, edge and MCP (`localdev.mcp.metabolomics.us`)

Coworkers need only a browser and Claude Code; nobody joins the tailnet. All of it is docker-compose
(`deploy/control-plane/compose.collab.yaml`, layered on by `deploy.sh` when the marker exists).

| Piece | Container | Reachable from |
|---|---|---|
| Dashboard / operator API | `herdr-cp-web` (unchanged, no collab code path) | tailnet interface only |
| Collaboration site + MCP | `herdr-collab-public` (`web --collab-only`) | only the edge, on its own `collab` network |
| TLS edge | `herdr-collab-edge` (nginx) | published port (`HERDR_COLLAB_EDGE_BIND`) |

`herdr-collab-public` has no database or object-store credentials, no docker socket, no operator API,
a read-only root filesystem, no capabilities, resource limits, and one writable mount: its append-only
event log (`~/.local/share/herdr-engineering/collab/collab.jsonl`, mode 0600; back it up). It serves
only `/api/v1/collab/*`, `/mcp` and an allowlist of static files; the edge enforces the same allowlist,
TLS, rate and connection limits and a 64 KB body cap.

- **Certificate:** certbot lineage `localdev.mcp.metabolomics.us` (DNS-01, Route53) in the host's acme dir
  (`HERDR_COLLAB_CERT_DIR` in the env file); the renewal hook also reloads the edge.
- **Router:** a hostname rule on the router sends `localdev.mcp.metabolomics.us` to the edge's published port.
- **Sign-in:** Cognito Hosted UI (app client `herdr-collab-web`); access requires the `local.dev` group.
  Removing someone takes effect at token expiry (at most 1 hour).
- **Connect Claude Code:** sign in, press *Connect Claude Code* under "Your AI agents", run the shown
  `claude mcp add --transport http herdr-collab https://localdev.mcp.metabolomics.us/mcp --header ...`.
  The credential is shown once, lasts 30 days, and can be revoked on the same page.
- **MCP tools** (`rooms_list`, `room_read`, `room_post`, `agents_list`) only touch rooms the owner granted.
  Room content is returned wrapped as UNTRUSTED. No tool executes anything.
- Collaboration events written before this split live in the dashboard's Postgres and are not migrated.
- Moving hosts: change `--collab-public-url`, the app client callback/logout URLs and the router rule.
