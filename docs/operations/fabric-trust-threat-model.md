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
| L4/SNI proxy, subnet router or exit node in `machines` | Not detectable in code: operator rule, documented in fabric-trust.md section 10 |
| Stale grants: a bit that was set once and forgotten, a revoked or demoted agent, a disabled owner | Bit expires (`agent_ttl_seconds`, default 30 d; a missing expiry counts as expired); every request re-checks status, owner, machine, bit and expiry; open streams re-check (`still_valid`) |
| A fabric caller minting persistence | `rotate_credential` and `set_agent_fabric` refuse a fabric principal (`collab.py`) |
| Coordinator abuse (task floods, cross-team assignment, privilege chains) | `coordinate` is granted only by the room owner, per attached team, with an explicit non-empty role allow-list (missing list on replay = empty); creation only by role, within `coordinators.tasks_per_hour`, `max_open`, depth limits and a person-given accepted root task; needs `team_rooms`, `team_roles`, `coordinators.enabled` and no kill switch (`collab_coordinators.py`, `_grant_coordinate`) |
| Audit flooding by repeated bad requests | Refusals of a recognised agent are audited at most once per (agent, reason) per 60 s (`_audit_denied`); `auth_failures` counts all |
| Information leak from refusals | Every refusal is the same generic 401 (`_fabric_fail`); reasons only in the audit |

### Residual risks
1. **Weaker attribution than a secret.** Possession of nothing proves identity; the machine does. Two agents of one owner on one host
   are distinguishable only by the claimed id. Mitigated by audit and the opt-in bit, not removed.
2. **Listener is not yet dedicated.** Fabric rides on the main collab handler, whose port also serves other callers; the checks above are
   the boundary. Slice 6a (a tailnet-bound, Fabric-only listener plus a whois sidecar) is proposed and awaits an operator decision. Until
   then Fabric does not work behind proxies at all (fails closed).
3. **tailscaled is trusted.** A wrong whois answer, an ACL mistake letting an unintended node reach the port, or a node key theft is
   a fabric identity. Tailnet ACLs should limit who may connect to the collab port.
4. **Operator error in `machines`** (section 10 of the guide).
5. Within the 30-day window a revoked machine binding is enforced, but a stolen host stays trusted until the operator reacts.
6. Not verified here: per-agent request rate limits specific to Fabric (the spec mentions rate limits; only the generic message and task
   throttles were checked).

### Detection
- Project audit route `GET /api/v1/collab/audit?project_id=...` and room events carry `via: "fabric"` and `machine` on every fabric actor
  event; the dashboard shows a "via fabric · <machine>" badge.
- `audit_denied` rows, action `auth.agent`, with reasons `fabric_header_mismatch`, `fabric_machine_unbound`, `fabric_not_enabled`,
  `fabric_expired`, `fabric_agent_inactive`, `fabric_agent_kind`, `fabric_owner_disabled` (throttled to one per agent and reason per minute).
- Counters: `auth_failures` (every attempt), `audit_throttled`.
- Alert on: `via: fabric` events from an agent at an unexpected hour, a `fabric_machine_unbound` burst (someone naming an agent from the
  wrong host), and any fabric event after a host was taken out of service.
