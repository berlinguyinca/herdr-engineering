# Collaboration service threat model: Fabric-trusted agent auth

No threat model for the collaboration service existed (`docs/operations/team-rooms.md` and the collaboration guide section 6 describe
controls only), so this one is new and covers the `Authorization: Fabric` scheme. Operator guide: [fabric-trust.md](fabric-trust.md).
Design: `docs/specs/fabric-trust-agent-auth-20261104/SPEC.md`.

## Fabric-trusted agent auth

### Assets
- The authority of every agent that has the `fabric` bit: its room grants (`read`, `post`, `execute`, `instruct`, `spawn_helpers`, and for a
  coordinator `coordinate`), and through them room content and task flow.
- Task integrity: who may create, accept and answer tasks, and the results other agents and people act on.
- The audit trail's attribution (`actor_agent_id`, `via`, `machine`).
- The tailscaled LocalAPI as the identity oracle.

### Trust boundary
A request is fabric only if the real TCP peer address (`client_address`, never a header) is a tailnet address (`100.64.0.0/10`,
`fd7a:115c:a1e0::/48`), tailscaled whois resolves it to a node whose FULL MagicDNS name is `<machine>.<tailnet_suffix>` for a listed
machine, the agent exists, is active, is not external or a helper, has its owner still approved, is bound to that same machine, and
carries an unexpired owner-set `fabric` bit. Trust is therefore in: the tailnet's node identity, tailscaled, the operator's
`machines` list, the host's own users, and the agent's owner.

### Attackers and what stops them

| Attacker | Mitigation (code) |
|---|---|
| Any process, any user, on an allow-listed host that names a fabric-bit agent bound to that host | Accepted trade-off (see residual). Limited by: per-agent opt-in bit and machine binding (`_fabric_agent_problem`, `collab_fabric_trust.py`), the agent's existing grants and ceilings (never admin/invite/grant), audit with `via`/`machine` (`_actor`, `collab.py`) |
| Compromised allow-listed host | Same blast radius as above plus every fabric-bit agent bound to it. Contain with `kill_switch`, clearing a bit (`set_agent_fabric`, takes effect on open streams via `_fabric_still_valid`), or removing the machine from `machines` (re-read per request; whois cache only caches identity) |
| Spoofed or proxied peer: `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `Tailscale-User-*`, `Via`, loopback or LAN source, `tailscale serve`, public edge | `_fabric_args` in `control_plane/api.py` offers no peer when any forwarding header is present and none on the `public` and `operator` handlers; `_real_peer` rejects loopback, unspecified, multicast and anything outside the tailnet ranges, and unwraps IPv4-mapped IPv6; `X-Herdr-Machine` is only compared, never trusted |
| Name confusion (a node called `bender.evil.net`, or only the first label matching) | Full-name match against `<machine>.<tailnet_suffix>` in `FabricPeerResolver.resolve`; `tailnet_suffix` is required when enabled |
| DNS rebinding: a browser page whose name resolves to the listener's tailnet address | The listener answers only to `Host: <listen_host>:<listen_port>` (421 otherwise, before routing and authentication; `_fabric_hosts`, `api.py`); a rebound `Origin == Host` pair therefore never reaches the service |
| Unauthenticated routes (enrollment, join, join-link, config) reached over the tailnet listener | `dispatch(fabric_listener=True)` makes them 404; they exist on the public listener only |
| Compromised `collab-public` reaching the host or operator plane through the `fabric` network's default route | Not closed by compose (a published port needs a non-internal network). Required host rules from `collab-fabric-firewall.sh` drop NEW connections from the fabric subnet in `DOCKER-USER` and `INPUT`; `deploy.sh` warns when they are absent |
| A local process on the collab host (or an `ssh -L`/`socat` relay, host-network container) | Not stopped: it connects from the host's own tailnet IP and is accepted as that machine when the host is in `machines`. Operator rule: no relays or L4 fronts on the collab host; omit it from `machines` unless its own agents need it |
| L4/SNI proxy, subnet router or exit node in `machines` | Not detectable in code: operator rule, documented in fabric-trust.md section 10 |
| Stale grants: a bit that was set once and forgotten, a revoked or demoted agent, a disabled owner | Bit expires (`agent_ttl_seconds`, default 30 d; a missing expiry counts as expired); every request re-checks status, owner, machine, bit and expiry; open streams re-check (`still_valid`) |
| A fabric caller minting persistence | `rotate_credential` and `set_agent_fabric` refuse a fabric principal (`collab.py`) |
| Coordinator abuse (task floods, cross-team assignment, privilege chains) | `coordinate` is granted only by the room owner, per attached team, with an explicit non-empty role allow-list (missing list on replay = empty); creation only by role, within `coordinators.tasks_per_hour`, `max_open`, depth limits and a person-given accepted root task; needs `team_rooms`, `team_roles`, `coordinators.enabled` and no kill switch (`collab_coordinators.py`, `_grant_coordinate`) |
| **Auto-join** (`auto_join: true`): any process on an allow-listed machine creates an agent and acts as it, with no token and no owner click | Accepted by operator decision (2026-11-07): the machine is the trust unit. Limited by: one default-off hot-reloaded switch; fabric listener only (404 elsewhere); whois-only auth that ignores `Authorization`; the created agent is a normal agent of `operator_user` with NO room permissions, never `coordinate` (room owners grant both), no credential to steal or rotate; 30 new agents/hour and 200 names (revoked included) per machine plus 1000 in total, kept apart from the operator's own agent quota; refusal audits throttled per (machine, reason) and capped at 500, renewals written once a day, so none of it can fill the capped event log; external and helper agents excluded; a fabric caller cannot set the bit or rotate credentials; revocation is immediate and a revoked (machine, name) is never resurrected (`fabric_join_revoked`); switch off, kill switch or `enabled: false` ends their Fabric auth (`fabric_auto_join_off`) |
| Audit flooding by repeated bad requests | Refusals of a recognised agent are audited at most once per (agent, reason) per 60 s (`_audit_denied`); `auth_failures` counts all |
| Information leak from refusals | Every refusal is the same generic 401 (`_fabric_fail`); reasons only in the audit |

### Residual risks
1. **Weaker attribution than a secret.** Possession of nothing proves identity; the machine does. Two agents of one owner on one host
   are distinguishable only by the claimed id. Mitigated by audit and the opt-in bit, not removed.
2. **The `fabric` docker network has a default route.** Compose cannot make it internal (publishing a port needs a non-internal network) and
   `enable_ip_masquerade: false` does not remove the route; it only stops NAT. Without the firewall rules a compromised `collab-public` can open
   connections to the host's own addresses (tailnet, LAN, host-network services), other containers' published ports and, where routed, the LAN. The
   rules are a REQUIRED operator step, kernel state that must be re-applied at boot; deploy only warns. Reboot ordering: without the docker drop-in
   `docker-after-tailscale.conf` the published port may not bind and the public collab site stays down.
   **The collab host trusts itself**: any local process connecting to the listener appears as the host's own tailnet IP.
3. **The dedicated listener shares the process with the public handler.** Fabric is accepted only on a tailnet-bound, Fabric-only listener
   that serves the collab routes alone; the public and operator handlers refuse it. A compromise of the collab process is still a compromise
   of both listeners. The whois answer comes from a sidecar that exposes only `GET /whois` over a unix socket, so `collab-public` never holds
   tailscaled's LocalAPI socket. Docker's published-port path must preserve the real source address (procedure in the guide, section 9);
   if it does not, Fabric fails closed.
4. **tailscaled is trusted.** A wrong whois answer, an ACL mistake letting an unintended node reach the port, or a node key theft is
   a fabric identity. Tailnet ACLs should limit who may connect to the collab port.
5. **Auto-join widens the allow-list's meaning.** With `auto_join` on, an allow-listed machine is trusted not only to run vouched
   agents but to mint new ones: any local process there can join under any free name and fill the per-machine cap of 200 and the total of 1000 (names are
   enumerable; revoke to clean up, but revoked names keep counting: a known limit). Room access still needs an owner's grant.
5a. **Operator error in `machines`** (section 10 of the guide).
6. Within the 30-day window a revoked machine binding is enforced, but a stolen host stays trusted until the operator reacts.
7. Not verified here: per-agent request rate limits specific to Fabric (the spec mentions rate limits; only the generic message and task
   throttles were checked).

### Detection
- Project audit route `GET /api/v1/collab/audit?project_id=...` and room events carry `via: "fabric"` and `machine` on every fabric actor
  event; the dashboard shows a "via fabric · <machine>" badge.
- `audit_denied` rows, action `auth.agent`, with reasons `fabric_header_mismatch`, `fabric_machine_unbound`, `fabric_not_enabled`,
  `fabric_expired`, `fabric_agent_inactive`, `fabric_agent_kind`, `fabric_owner_disabled`, `fabric_auto_join_off` (throttled to one per agent and reason per minute).
- Join events: `agent_enrolled` with `origin: "fabric_join"` and `via: "fabric"`; refusals are `audit_denied`, action `agent.fabric_join`
  (`fabric_join_revoked`, `fabric_join_rate_limited`, `fabric_join_machine_cap`, `fabric_join_total_cap`, `fabric_join_machine_rebound`),
  at most one per (machine, reason) per 5 minutes and 500 in the log's lifetime. The dashboard marks auto-joined agents.
- Counters: `auth_failures` (every attempt), `audit_throttled`.
- Alert on: `via: fabric` events from an agent at an unexpected hour, a `fabric_machine_unbound` burst (someone naming an agent from the
  wrong host), and any fabric event after a host was taken out of service.
