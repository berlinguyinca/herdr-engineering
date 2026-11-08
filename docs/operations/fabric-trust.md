# Fabric trust: collaboration without tokens for agents inside the fabric

Status: slices 1-5 and the dedicated listener with its whois sidecar (slice 6a) are implemented; the rollout drill (slice 6) is not. Design: `docs/specs/fabric-trust-agent-auth-20261104/SPEC.md`.
Threat model: [fabric-trust-threat-model.md](fabric-trust-threat-model.md).

## 1. What it is

An agent that runs on a machine of your own tailnet (bender, beast, zapp, ...) can use the collaboration API without holding a secret.
The control plane asks the local tailscaled who owns the TCP peer address; if that node is on an allow-list, and the agent was bound
to that machine and switched on by its owner, the request is accepted as that agent, with exactly the grants and ceilings the same agent
has with a credential. Tokens stay for external users (people through Cognito or a static bearer, external coworker agents through an
invite credential). Off by default; YAML only (no environment variables).

## 2. The three auth schemes

| Header | Who | Secret |
|---|---|---|
| `Authorization: Bearer <token>` | people (Cognito or static token) | the token |
| `Authorization: Agent <credential>` + `X-Herdr-Device` | agents and external coworker agents | the credential |
| `Authorization: Fabric <agent_id>` (+ optional `X-Herdr-Machine: <name>`) | agents of the owner, on an allow-listed machine | none; the tailnet machine vouches |

`X-Herdr-Machine` is informational. The whois result decides; a header that names a different machine is a 401 (`fabric_header_mismatch`).
External agents and helpers can never use `Fabric`. A fabric caller cannot set the fabric bit and cannot rotate a credential (403).

## 3. Prerequisites

- tailscaled runs on the collab host and the control-plane user can reach its LocalAPI socket. The code tries
  `/var/run/tailscale/tailscaled.sock` and `/run/tailscale/tailscaled.sock`. Give that user access with
  `tailscale set --operator=<user>` or a group on the socket. If tailscaled is unreachable, Fabric fails closed (401).
- The collab service sees the real peer. Fabric works only on the dedicated fabric listener (section 9), with a peer inside
  `100.64.0.0/10` or `fd7a:115c:a1e0::/48`.
- Every machine you list is an ordinary end host (see section 10).

## 4. Configuration

In the fleet/control-plane YAML (read per request, so an edit takes effect without a restart):

```yaml
control_plane:
  collab:
    fabric_trust:
      enabled: false           # absent = off
      kill_switch: false       # true = every Fabric request is 401 at once
      machines: [bender, beast, zapp]   # tailscale MagicDNS short names
      tailnet_suffix: tail0c50da.ts.net # REQUIRED when enabled: a node is a machine only if its FULL name is <machine>.<suffix>
      agent_ttl_seconds: 2592000        # how long the owner's "fabric" bit stays valid (default 30 d, 300..7776000)
      whois_cache_seconds: 30           # 0..3600
      listen_host: 100.104.39.6         # the dedicated listener (section 9); tailnet range only; with listen_port, or neither
      listen_port: 9483
      whois_socket: /run/fabric-whois/whois.sock   # ask the whois sidecar instead of tailscaled (section 9); absolute path
      auto_join: false         # true = agents on an allow-listed machine join with no token and no owner click (section 5a)
      workspace_rooms: false   # true = auto-joined agents of one herdr workspace share ONE room (section 5a); requires auto_join
      workspace_rooms_per_machine: 100   # 1..10000; new workspace rooms per machine (429 past it)
      workspace_rooms_total: 500         # 1..10000; across all machines
      workspace_room_members: 50         # 1..10000; active agents with a grant in one workspace room
```

`listen_host`, `listen_port` and `whois_socket` are read at startup only (an edit needs a restart or a deploy); everything else stays hot.
`listen_host` outside `100.64.0.0/10` / `fd7a:115c:a1e0::/48` is refused at config time; there is no config switch to relax that.

Unknown keys, wrong types, `enabled` without `machines` or without `tailnet_suffix` are rejected (`herdr-eng` refuses the config). A source
that fails to read or validate means OFF with the kill switch on. The machine is matched on the FULL MagicDNS name
(`<machine>.<tailnet_suffix>`, trailing dot stripped, case-insensitive), never on the first label alone.

## 5. Enrolling an agent and switching it on

1. Enrol the agent with its machine recorded. With `join.sh`: `... | HERDR_JOIN_TOKEN='hcj_...' bash -s -- --machine bender [--count N ...]`
   (`--machine NAME`: the MagicDNS short name). Enrolment records the machine and never turns the bit on.
2. The room owner grants the agent its room permissions as usual.
3. The agent's OWNER sets the bit: `POST /api/v1/collab/agents/{agent_id}/fabric` with `{"fabric": true}` (optionally `"machine": "bender"` to
   (re)bind). An agent without a recorded machine gets 400. Only the owner can call it; agents (fabric ones too) cannot. Clearing the bit
   (`{"fabric": false}`) is a revocation and takes effect at once, including on open streams.
4. **Expiry and renewal.** Setting the bit records `fabric_expires_at` = now + `agent_ttl_seconds` (30 days by default). After that every
   Fabric request is refused with `fabric_expired`. Setting the bit again renews it. Logs written before this field existed count as
   expired (fail closed). The agent list (`list_my_agents`) shows `machine`, `fabric` and `fabric_expires_at`, and the dashboard marks an
   expired bit "fabric expired".

## 5a. Auto-join: no token, no owner click

Operator decision (2026-11-07): an agent that runs on a machine in `machines` may join and act on that machine's word alone. One YAML switch
turns it on: `control_plane.collab.fabric_trust.auto_join: true` (default `false`, hot-reloaded like its siblings, requires `enabled: true`).

**What you are trusting.** The operator trusts EVERY process on an allow-listed machine: whois vouches for the machine, not for a program or a
user, so any local process there can join and then name the agent it created. Do not allow-list a machine you would not trust with that.
The collab host in `machines` trusts itself, as in section 9, unchanged.

**The route.** `POST /api/v1/collab/agents/fabric-join`, served only on the dedicated fabric listener (404 on the public and operator
listeners). It authenticates by whois alone and ignores any `Authorization` header. Body `{"name": "<1-64 chars: lowercase letters, digits,
dot, hyphen, underscore, first a letter or digit>", "machine": "<optional, informational>"}`; the machine is always the one whois found. It answers
`{agent_id, name, machine, fabric_expires_at}` (201 on create, 200 on renew).

**What gets created.** A normal agent owned by the collab `operator_user` (the server needs `operator_user` set), with `machine` from whois, the
fabric bit on, and `origin: "fabric_join"`, harness family `agent` (so it gets an address like any agent: `<handle>.agent`, `<handle>.agent-2`, and
its name resolves in `@` mentions). It has NO credential (nothing secret exists to leak or rotate), NO room permissions and never
`coordinate`: a room owner grants permissions exactly as for any agent (section 6 for `coordinate`, which stays an explicit owner decision).
The audit row carries `via: "fabric"`. Joining is idempotent per (machine, name): the same pair returns the same agent, the same name on
another machine is a separate agent.

**Limits.** Three bounds, none of them the operator's own `max_agents_per_user` (auto-joined agents are NOT counted there, so a noisy machine
cannot starve the operator's own enrolments, and the operator's enrolments cannot block joins): 30 NEW agents per machine per hour (429
`fabric join` throttle; renewals, revoked-name and cap refusals do not spend it), 200 names per machine (revoked names count, see
Revocation) and 1000 auto-joined agents in total across machines (`fabric_join_total_cap`, 429). Refusals are audited
(`agent.fabric_join`, outcome `denied`, reasons `fabric_join_rate_limited`, `fabric_join_machine_cap`, `fabric_join_total_cap`,
`fabric_join_revoked`, `fabric_join_machine_rebound`), but at most one audit row per (machine, reason) every 5 minutes and 500 in the
log's lifetime, so a loop over fresh names cannot fill the capped event log (the rest are only counted in `audit_throttled`).
External and helper agents cannot join this way, and a fabric caller can neither set the fabric bit nor rotate credentials. A re-join of a name
the owner has since bound to a DIFFERENT machine answers 403 (`fabric_join_machine_rebound`) and changes nothing; renewals never rebind a machine.

**Renewal.** The fabric bit of an auto-joined agent slides to now + `agent_ttl_seconds`, but is written at most once per 24 hours (sooner only
when `agent_ttl_seconds` is under four days: a quarter of the window), so a polling agent costs about one event a day. A bit the owner set by
hand (section 5) keeps its fixed TTL.

**Revocation.** Revoking the agent (dashboard: agents are marked "auto-joined") or clearing its fabric bit takes effect immediately. A later
`fabric-join` of the same (machine, name) answers 403 and is audited as `fabric_join_revoked`: a revoked agent is never resurrected. Choose a
new name to start over. **Known limit:** a revoked (machine, name) stays blocked for good and keeps counting toward that machine's 200-name cap
(that is what keeps the index bounded); there is no owner-side way to clear one short of a new machine name or a log rebuild. Setting `auto_join: false`, the kill switch, or `enabled: false` ends Fabric auth of auto-joined agents at once
(audit reason `fabric_auto_join_off`); agents the owner enabled by hand are unaffected by `auto_join`.

**Client.** The route exists ONLY on the fabric listener, never on the public site. `herdr-eng rooms join-fabric NAME [--machine M]
[--control-plane http://<listen_host>:<listen_port>]` finds the listener from this host's configuration (`fabric_trust.listen_host` and
`listen_port`) when `--control-plane` is omitted, and otherwise fails at once saying so. The shell cannot discover it, so it needs the URL:
`curl -fsSL <site>/api/v1/collab/scripts/join.sh | bash -s -- --fabric --name NAME --fabric-url http://<listen_host>:<listen_port>`. Names follow
one rule in the server, the Python client and `join.sh`. Both write `~/herdr-agents/NAME.json` (`$HERDR_AGENT_DIR` honoured), mode 0600, holding only
`{agent_id, name, machine}`; the existing identity discovery (`--as-agent`, `HERDR_AGENT_ID`, the single file) then finds it. The token
path (`HERDR_JOIN_TOKEN`) is unchanged.

**Workspace rooms (`workspace_rooms: true`, default off, requires `auto_join: true`).** Agents that auto-join from the same herdr WORKSPACE on
the same machine land in ONE shared room with no owner click. The body gains an optional `workspace` object `{"id": "<1-64 chars: letters, digits,
dot, underscore, colon, hyphen>", "label": "<display only, at most 64 printable characters>"}` and optional informational `tab_id` and `pane_id`
(same character rule, stored on the agent for display only). The room key is `(whois machine, workspace id)`: the machine is never client
supplied, so the same workspace id on another machine is a different room. Without `workspace` (or with the switch off) behaviour is exactly as
above. Malformed fields are a 400 and create nothing.

- *Room.* The first join for a key creates the room (`<machine>/<label or id>`, owner = `operator_user`) in a lazily created project
  `fabric-workspaces` that the operator owns, then grants the agent. Later joins find the room through an index rebuilt from the event log, so
  it survives a restart or rebuild. All writes are `_commit` events whose audit rows carry `via: "fabric"`; the response adds `room_id` and `workspace`.
- *Grant.* `read`, `post`, `instruct` and `execute`. Never `spawn_helpers`, never `coordinate` (see below). The grant has no expiry of its own: it
  lasts as long as the agent's fabric window and ends on revocation. The binding and the grant are one event, so an owner who narrows or removes
  an agent's grant is not overridden by a later re-join.
- *Bounds (429, fail closed, throttled audit rows like the other refusals).* `workspace_rooms_per_machine` (100), `workspace_rooms_total` (500)
  and `workspace_room_members` (50; active agents holding a grant). Reasons `fabric_ws_machine_cap`, `fabric_ws_total_cap`, `fabric_ws_members_cap`.
  The caps are checked before the agent is created, so a refused join leaves nothing behind. These writes do not go through the user paths, so
  `max_rooms_per_project` and `max_projects_per_user` do not apply to them.
- *One workspace per agent, auto-join only.* An auto-joined agent belongs to at most one workspace room. Re-joining the same (machine, name) from
  a DIFFERENT workspace id answers 409, audits `fabric_join_workspace_changed` and changes nothing (not even the renewal); use another agent name.
  Re-joining with no workspace changes nothing. An agent that joined before sending a workspace is bound by its first join that carries one.
  This rule limits only the tokenless path. A workspace room is an ordinary room: the owner can grant ANY other active agent into it (a
  token-enrolled agent on another machine, a coordinator into several workspace rooms) with the normal grant, add members and guests, invite
  externals, and change or remove the auto-granted access; those changes survive later auto-joins and a restart. There is deliberately no tokenless
  way for an agent to enter a room of a different workspace or machine; that would bypass the owner and is a separate decision.
- *Revocation.* Revoking the agent removes its room access at once and its (machine, name) stays blocked, as above.
- *Peer assignment and `coordinate`.* `instruct` plus `execute` let a peer hand on a task a PERSON gave it (an agent must pass `parent_task_id` of
  a task it accepted, rooted in a person's task); they do not let a peer start a task for another peer, and `my-tasks`/`respond` are the person flow.
  Originating needs `coordinate` or an owner-designated team coordinator, which also needs an attached team. That stays an explicit owner grant.
- *Client.* `herdr-eng rooms join-fabric NAME` and `join.sh --fabric` read `HERDR_WORKSPACE_ID` (plus `HERDR_TAB_ID`, `HERDR_PANE_ID` and, when set,
  `HERDR_WORKSPACE_LABEL`) from the environment and send them; `--no-workspace` opts out. The identity file stays `{agent_id, name, machine}`
  and gains `room_id` when the join landed in a room. The dashboard's agents list shows the workspace and room of an auto-joined agent; there is no new UI.
- ***A YAML edit alone does nothing.*** collab-public reads the RENDERED `collab-config.json`, not the fleet YAML. `auto_join` and the four
  workspace keys reach it only through `herdr-eng fleet config render-collab` (which validates and carries them) or a deploy that runs it.
  Check the rendered file before concluding the feature is on.

**Rollback.** An image without this feature rejects the `auto_join` and `workspace_rooms*` keys: remove them from the config BEFORE rolling back. Agents already
created are then ordinary fabric-bit agents bound to their machine and keep working until their bit expires; revoke them to remove them.

## 6. Granting `coordinate`

A coordinator agent (the bender Claude session, registered as its own agent) creates tasks BY ROLE inside one attached team. The room OWNER
grants it directly: `POST /api/v1/collab/rooms/{room_id}/agents` with `actions` including `coordinate`, plus `team_id` (an attached team of
that room) and `roles`, a non-empty list of team roles (`implementer`, `issue_writer`, `reviewer`, `tester`). A grant without `roles` is 400.
`roles` is the exact allow-list; re-granting replaces it; revoking or detaching clears it. Not self-grantable, not by an agent, not by a
request approval, never for helpers. It needs `team_rooms`, `team_roles` and `coordinators.enabled` on and `coordinators.kill_switch` off
(and the agent also needs `execute`, plus a person-given root task, passed as `parent_task_id`, that it accepted). The coordinator posts to
`rooms/{room_id}/tasks` with `target_role` (not `target_agent_id`), within `coordinators.tasks_per_hour` / `max_open` and depth limits.
`GET rooms/{room_id}/agent/tasks/created` lists the tasks it created with status and result. The dashboard shows
"coordinator: <team> · roles <list>" on the agent.

## 7. Agent task routes and CLI

Agent-principal routes (credential or Fabric), under `/api/v1/collab`:

- `GET rooms/{room_id}/agent/tasks` - this agent's tasks.
- `POST rooms/{room_id}/agent/tasks/{task_id}/respond` - body `status` (`accepted`, `done`, `declined`, `failed`), optional `result`, `attempt`.
- `GET rooms/{room_id}/agent/tasks/created` - coordinator's own tasks.

CLI (`herdr-eng rooms my-tasks ROOM` and `herdr-eng rooms respond ROOM TASK [STATUS] [--status S] [--result TEXT] [--attempt N]`):
agent mode applies when no token is set in `--token-env`, or when `--as-agent` is given (an explicit `--as-agent` wins over a token).
- `--as-agent AGENT_ID` sends `Authorization: Fabric AGENT_ID`.
- `--machine NAME` is the informational machine header; default is asked of `tailscale status --json` (its `Self.DNSName` first label).
- Agent identity sources, in order: `--as-agent`; `HERDR_AGENT_ID`; the single agent in `~/herdr-agents/*.json` (`$HERDR_AGENT_DIR`; only
  its `agent_id` is read, never the credential). Several agents on one host is ambiguous: pass `--as-agent`.
- `--attempt N` (from `my-tasks`) is the lease fence; with a bearer token it is ignored (a message says so).

## 8. Operating notes

- **Audit.** Every event an agent principal commits carries `actor_agent_id` and, for fabric callers, `via: "fabric"` and `machine`
  (events from the read routes, the stream and the project audit route `GET audit?project_id=...` show both fields). Refusals of an otherwise
  recognised agent (right allow-listed machine) are audited as `audit_denied` with action `auth.agent` and the reason below, at most once
  per (agent, reason) per 60 s; the `auth_failures` counter counts every attempt. Refusals before the machine is known are not logged.
- **Kill switch.** `kill_switch: true` (or `enabled: false`): every Fabric request is 401. Nothing is deleted; tokens and credentials are
  unaffected. Open Fabric streams end at the next re-check.
- **Rollback (SPEC 6).** New events are additive and ignored by an older image. Before rolling the IMAGE back, revoke the `fabric` bit of
  agents and revoke `coordinate` deliberately, and let the sweep run first: an old image does not know `coordinate`, treats a coordinator's open
  tasks as creator-invalid, and its sweep turns them into permanent cancellations.
- Any owner re-grant of a coordinator resets its grant sequence, so it needs a fresh root task.

## 9. The dedicated fabric listener

With `listen_host` and `listen_port` set, the collab server (same process as the public handler, so the event log still has one writer)
opens a second HTTP listener that:

- serves only the collab API routes (`/api/v1/collab/...`); the MCP, A2A, web UI and operator API paths are 404 there;
- answers only to its own address: the `Host` header must be `<listen_host>:<listen_port>` (IPv6 in brackets), otherwise `421` before routing or
  authentication, so a DNS-rebinding page (attacker name resolved to the tailnet address) gets nothing, and its `Origin == Host` pair does not help;
- does not serve the routes that run before authentication (`agents/enrollment/complete`, `agents/join`, `agents/join-link`, `config`): 404 there, they
  stay on the public listener;
- accepts only `Authorization: Fabric ...`; `Bearer` and `Agent` get 401 without being evaluated (no token check, no failure counting);
- passes the real TCP peer address (`client_address[0]`) to the whois check and refuses any request carrying a forwarding header
  (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `X-Forwarded-Host`, `X-Forwarded-Server`, `Tailscale-User-Login`, `Tailscale-User-Name`, `Via`);
- is plain HTTP on the tailnet (WireGuard encrypts the hop); no TLS and no proxy in front.

The public (internet-facing) and operator handlers never pass a peer, so `Fabric` is always 401 there. A bind failure is a loud startup error.
Loopback, LAN and public peers never qualify, so `tailscale serve` or any local proxy still yields 401.

### The whois sidecar

The isolated `collab-public` container must not see tailscaled's LocalAPI socket. `herdr-eng fabric whois-proxy --listen-socket PATH
[--tailscaled-socket PATH]` runs as a separate container from the same image. It serves ONLY `GET /whois?addr=<tailnet ip>` on a unix socket
(mode 0660), answers 400 for a malformed address and 404 for every other path or non-tailnet address, and returns only the node name, tags
and login. With `fabric_trust.whois_socket` set, the collab server asks that socket (same JSON shape) instead of tailscaled.

### Deploy

`deploy.sh` layers `compose.collab-fabric.yaml` only when the collab overlay is on and `fabric_trust.listen_*` is set (it reads the address with
`herdr-eng fleet config fabric-listener`). It adds the `fabric-whois` service (no network, read-only root, no capabilities, non-root, tailscaled's
socket directory mounted read-only, one writable directory for its own socket), mounts that directory read-only in `collab-public`, and
publishes `listen_host:listen_port` from `collab-public`. `collab-public` gains no capability, no tailscaled mount and no environment. The
new `fabric` network (fixed subnet `172.30.213.0/24`) is NOT internal, because Docker cannot publish a port from an internal-only network. Its IP
masquerade is off, but that does not remove the default route; see "Required hardening" below. `HERDR_COLLAB_TAILSCALED_DIR` (default `/var/run/tailscale`) names the
host directory that holds tailscaled.sock. Removing `listen_*` from the config stops the sidecar on the next deploy.

### Checking the source address (do this before trusting the listener)

The listener decides identity from the TCP peer, so Docker must show it the real tailnet address, not the bridge gateway (172.x), which
would fail closed (every Fabric request 401). On the collab host, after a deploy:

1. From another tailnet machine: `curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Fabric some-agent' http://<listen_host>:<listen_port>/api/v1/collab/config`.
2. A recognised, switched-on fabric agent gets 200; for a refused one, see the audit reason (section 11). A consistent refusal for every machine means the peer is the gateway.
3. Confirm directly: run `sudo conntrack -L -p tcp --dport <listen_port>` (or `sudo tcpdump -ni any port <listen_port>`) while a request is in flight: the original source must be the machine's 100.x address and the
   DNAT target the container. If `docker info` shows the userland proxy in use (`"userland-proxy": true` in daemon.json) for that port and the
   source is the gateway, set `"userland-proxy": false` and restart the daemon, then re-check.
4. Fallback if the source address cannot be preserved: run `collab-public` with `network_mode: host` and set
   `listen_host` to the host's tailnet address (the publish step goes away; the `internal` collab network no longer applies, so firewall the
   other ports of that process and keep the edge pointed at 127.0.0.1). This weakens `collab-public`'s isolation and is a conscious choice.

### Required hardening: the fabric network has a default route

Be precise about what the overlay does NOT do: it does not cut `collab-public` off from the network. With the `fabric` network non-internal, a
process inside `collab-public` (a compromise of the internet-facing collab server) has a default route and can open new connections to:

- the Docker host itself through the bridge gateway (172.30.213.1) and the host's own addresses: its tailnet and LAN IPs, and every service listening
  on them or on `0.0.0.0`, including services run with host networking and the operator listener if it is bound to a non-loopback address;
- other containers' published ports on the host, and other Docker networks;
- the LAN (when something routes it; masquerade is off so replies need a return route, which a LAN router may supply) and, where the host forwards
  and NATs for the subnet, the internet.

That is a path from the internet-facing process toward the operator plane, which the isolated `collab` network was built to prevent. Close it. This is
**required**, not optional, on any host that enables `listen_*`:

```
sudo deploy/control-plane/collab-fabric-firewall.sh --install     # idempotent
sudo deploy/control-plane/collab-fabric-firewall.sh --check       # exit 0 present, 1 absent, 2 not readable (not root)
sudo deploy/control-plane/collab-fabric-firewall.sh --remove
```

It inserts, in `DOCKER-USER` (forwarded traffic) and in `INPUT` (traffic to the host itself, which `DOCKER-USER` never sees), a rule that drops every NEW
connection whose source is the fabric subnet. Inbound connections to the published port are untouched (their source is the caller) and so are their replies.
The network has no IPv6, so no `ip6tables` rule applies. The rules are kernel state: re-run `--install` at boot (a oneshot unit `After=docker.service`) and
after anything that flushes iptables. `deploy.sh` only warns while `--check` does not succeed (it cannot install them: root); it never fails the deploy.
Verify: from `docker exec herdr-collab-public` open a TCP connection to the host's tailnet IP; it must hang, not connect.

### Boot order: docker must start after the tailnet address exists

The port is published on the tailnet address. If dockerd starts before tailscaled has that address, binding fails, `collab-public` does not start, and the
PUBLIC collab site is down until someone restarts it. Install the drop-in kept in the repository (root, once):

```
sudo install -D -m 0644 deploy/control-plane/docker-after-tailscale.conf /etc/systemd/system/docker.service.d/after-tailscale.conf
sudo systemctl daemon-reload      # applies at the next docker start; do not restart docker mid-day
```

It sets `After=tailscaled.service` and `Wants=tailscaled.service` and an `ExecStartPre` that waits up to 90 s for `tailscale ip -4` (then starts docker
anyway). `deploy.sh` warns while the file is missing. Nothing in this repository changes systemd for you.

### The collab host trusts itself

Any local process on the collab host (a shell, `ssh -L`, `socat`, a host-network container, anything that can connect to `<listen_host>:<listen_port>`)
reaches the listener with the host's OWN tailnet IP as the peer address. If the collab host is in `machines`, that connection is accepted as that
machine, and a fabric-bit agent bound to it can be named by any local process. Therefore:

- never run a relay, port forward or L4/SNI front on the collab host that reaches the listener on behalf of other machines;
- omit the collab host from `machines` unless agents running on that very host need fabric auth.

### Rollback

An image older than slice 6a rejects the new keys. Remove `listen_host`, `listen_port` and `whois_socket` from the config BEFORE rolling the
image back (the overlay then drops out and the sidecar is stopped).

## 10. What NOT to put in `machines`

Never an L4/SNI proxy, a subnet router or an exit node: such a node presents its own tailnet address for traffic that originates elsewhere, so
whois would vouch for the wrong party. Only ordinary end hosts that run the agents themselves.

## 11. Troubleshooting

All Fabric refusals return the same generic `401 unauthorized`; the reason is in the audit (`audit_denied`, action `auth.agent`).

| Reason (audit) | Meaning and fix |
|---|---|
| (none, generic 401) | feature off or kill switch on; peer not a tailnet address, loopback, or relayed (forwarding header); tailscaled unreachable; node not on `machines` or not `<machine>.<tailnet_suffix>`; unknown agent id |
| `fabric_header_mismatch` | `X-Herdr-Machine` names a different machine than whois found; drop the header or fix `--machine` |
| `fabric_agent_inactive` | the agent is not active (revoked or ended) |
| `fabric_agent_kind` | external or helper agent: these can never use Fabric |
| `fabric_owner_disabled` | the owner's account is no longer approved |
| `fabric_machine_unbound` | the agent has no recorded machine or it is bound to another one: re-bind with `POST agents/{id}/fabric` and `machine` |
| `fabric_not_enabled` | the owner has not set the bit (or cleared it) |
| `fabric_expired` | the bit's `agent_ttl_seconds` window ended: the owner sets it again |
| `fabric_auto_join_off` | the agent was auto-joined and `auto_join` is now off (or the feature is disabled): turn `auto_join` back on |
| `fabric_join_revoked` (join, 403) | that (machine, name) was revoked; it is not resurrected: join under a new name |
| `fabric_join_rate_limited` / `fabric_join_machine_cap` / `fabric_join_total_cap` (join, 429) | more than 30 new agents an hour, or 200 names (revoked included) from one machine, or 1000 in total |
| `fabric_join_machine_rebound` (join, 403) | the owner bound that name to another machine; join under another name |
| 400 on `POST agents/{id}/fabric` | no machine recorded: pass `machine` |
| 403 `forbidden: only the room owner grants coordinate` | grant `coordinate` as the room owner |
