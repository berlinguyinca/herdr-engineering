# Fabric-trusted agent auth for collaboration rooms

Status: DRAFT for operator review, 2026-11-04. No code written. Branch `docs/fabric-trust-agent-auth`.

## 1. Requirement

No collaboration token for any harness INSIDE the fabric (Claude, Pi, Codex, OpenCode on bender, beast, mac, zapp, ...).
Tokens remain for EXTERNAL users only (humans via Cognito/static bearer, external coworker agents via invite credential).
Driving use case: a coordinator agent (the bender Claude session) fans jobs out to agents on other hosts as room tasks
(assign -> accept -> respond done+result), with lease/escalation, visible in the dashboard.

## 2. Today (verified in code)

- `CollabService.authenticate()` (`control_plane/collab.py`) accepts `Bearer` (human) or `Agent <credential>` + `X-Herdr-Device`.
- `herdr-eng rooms ...` (`rooms_client.py`) only ever sends `Bearer` from `--token-env`.
- Agents already have a task path: `tasks_inbox`, `update_task` (accept/done/failed, lease, attempt fence, replay-safe) and
  `create_task` with the `instruct` grant. It needs the per-agent `Agent` secret.
- `my-tasks`, `respond`, `assign`, `coordinator*`, `attach`, `role-policy` are person-only/owner-only (`_tr2_person` -> 404 for
  agents; `_tr_owner`). Role routing and escalation already treat attached agents as holders (`_tr2_holder_keys`).
- Coordinators are operator-owned agent or opted-in external person (`collab_coordinators.py`).

## 3. Design

### 3.1 Transport identity (decision: tailscale whois)
A request is "from the fabric" only if the TCP peer address resolves, via `tailscale whois --json <peer-ip>` (local tailscaled
LocalAPI, cached 30 s), to a node whose MagicDNS name is in `control_plane.collab.fabric_trust.machines`. The control plane
must see the real peer: the fabric listener binds the tailnet interface directly; `X-Forwarded-For` is never consulted for
this decision. A request arriving through any proxy/public edge is never fabric-trusted. Hosts off the tailnet (e.g. mac off
network) are not eligible; they use their tokens.

### 3.2 New auth scheme
`Authorization: Fabric <agent_id>` plus `X-Herdr-Machine: <name>` (informational; the whois result is authoritative; a mismatch
is a 401). `authenticate()` resolves it to the SAME `Principal("agent", ...)` as a credential would, after checking:
1. flag `fabric_trust.enabled` true and `kill_switch` false (read per request, fails closed);
2. whois node in the allow-list;
3. agent exists, `status == active`, owner still `_ok`, not `external`, not a helper;
4. agent's enrollment records `machine` == whois node (new field, set at enroll via join.sh / `agent enroll`; agents without
   it cannot use Fabric auth);
5. the agent has the new per-agent bit `fabric: true` (granted by the owner, revocable).
Same grants, same ceilings, same audit trail; audit rows add `via: fabric`, `machine`. `still_valid()` re-checks the same
conditions on long streams. Externals and helpers can never use `Fabric`.

Threat note: anything running as any user on an allow-listed host can name any `fabric: true` agent bound to that host. That is
the accepted trade-off of B (weaker per-agent attribution than a secret). Mitigations: per-agent opt-in bit, machine binding,
allow-list, rate limits, audit, kill switch, and agents cannot be granted admin/invite/grant ever (existing ceiling).

### 3.3 Agent-callable task API
Thin agent-principal routes (do not loosen the human routes):
- `GET /rooms/{r}/agent/tasks` (from `tasks_inbox`), `POST /rooms/{r}/agent/tasks/{id}/respond` -> `update_task`.
- CLI: `herdr-eng rooms my-tasks|respond` auto-selects agent mode when it runs inside a herdr agent pane
  (`HERDR_AGENT_ID` from the pane env; no secret) and no token is set.

### 3.4 Coordinator agent
New agent action `coordinate` (not in `AGENT_ACTIONS` default; owner-only grant, never self-grantable, never by another agent).
A coordinator agent may create tasks BY ROLE within its own attached team, using the existing `_tc_check_body` limits
(`tasks_per_hour`, `max_open`, max depth, role allow-list). It may not set lease/policy, edit roles, attach/detach, or
read other rooms. Results flow back: assignee `respond done` -> task result readable by the creator and the owner; dashboard
shows the task and escalation unchanged. The bender Claude session is registered as its own agent (`bender-coordinator`).

Implementation notes (slice 4 hardening after review):
- The grant must carry `roles`: a non-empty list of team roles (subset of `TEAM_ROLE_ACTIONS`). It is stored in the `agent_granted`
  event (`coordinate_roles`) and is the coordinator's exact role allow-list. A grant without `roles` is 400. Re-granting replaces
  the list; revoke or detach clears it. An old event without `roles` replays as an EMPTY list (fail closed: owner must re-grant).
- It needs a person-given root task (`parent_task_id`, accepted by the agent) and the `execute` action on the agent.
- It works only while `team_rooms`, `team_roles` and `coordinators.enabled` are on and `kill_switch` is off. With the kill switch
  on, tasks already created become creator-invalid.
- Caps (`tasks_per_hour`, `max_open`, items) are per room/team/agent. The bound across rooms is the generic task throttle.
- `instruct` plus `coordinate` agents lose direct `target_agent_id` assignment while the flag is on (coordinators assign by role).
- Any owner re-grant resets `granted_seq`, so the coordinator needs a fresh root task.
- A granted coordinator counts as assistant-kind in the chain/loop guards, like a designated coordinator.

### 3.5 CLI/config
- `rooms ...` resolution order: `--token-env` token if set; else fabric agent mode if eligible; else error as today.
- `relay join` likewise. YAML only, no env vars for config:
```yaml
control_plane:
  collab:
    fabric_trust:
      enabled: false           # absent = off
      kill_switch: false
      machines: [bender, beast, zapp]   # tailscale MagicDNS short names
      tailnet_suffix: tail0c50da.ts.net # REQUIRED when enabled: a node is a machine only if its FULL name is <machine>.<suffix>
      agent_ttl_seconds: 2592000        # how long the owner's "fabric" bit stays valid (default 30 d, 300..7776000)
      whois_cache_seconds: 30
```

Security rules (review of 75689743):
- **Listener.** `Authorization: Fabric` is honoured on the MAIN collab handler only. The `public` (internet-facing, `/mcp`,
  `/a2a`) and `operator` handlers never pass a peer, so Fabric is always 401 there. The peer must also be inside
  `100.64.0.0/10` or `fd7a:115c:a1e0::/48`; loopback, LAN and public addresses are never fabric. Any forwarding header
  (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `Via`, ...) also refuses Fabric (defence in depth).
- **Never put an L4/SNI proxy, a subnet router or an exit node in `machines`.** Such a node presents its own tailnet address for
  traffic that originates elsewhere, so whois would vouch for the wrong party. `machines` must be ordinary end hosts only.
- **Real fix (operator decision pending):** a dedicated listener bound to the tailnet interface that serves the collab API and
  nothing else. Until then the main listener plus the checks above is the accepted boundary.
- **Machine identity** is the FULL MagicDNS name (trailing dot stripped, case-insensitive) equal to `<machine>.<tailnet_suffix>`;
  the first DNS label alone is never enough.
- **Expiry.** When the owner sets the bit the `agent_fabric_set` event records `fabric_expires_at` (now + `agent_ttl_seconds`).
  `authenticate` and `still_valid` refuse after it (`fabric_expired`); setting the bit again renews it; clearing it removes it.
  Events without the field (older logs) count as expired: fail closed. The agent list view shows `fabric_expires_at`.
- **Audit.** Refusals of an otherwise-recognised agent are audited at most once per (agent, reason) per 60 s
  (`_audit_denied` throttle); the `auth_failures` counter still counts every attempt. Every event an agent principal commits goes
  through `_actor`, so `via`/`machine` are always present for fabric callers (including `helper_task`).
- **whois for IPv6** brackets the address (`[addr]:1`).

## 4. Slices (each: failing test first, real server, no mocks)
1. Peer identity: whois resolver against real tailscaled in test via a local socket fixture; allow-list; unit + integration.
2. `Fabric` scheme in `authenticate()` + `still_valid()`, enroll `machine` field, `fabric` bit, audit fields, spoof negatives.
3. Agent task routes + CLI auto agent-mode.
4. `coordinate` action + coordinator-agent create-by-role + limits + audit.
5. Dashboard shows `via: fabric`/machine on events; docs (`docs/operations/fabric-trust.md`), threat-model update.
6a. Dedicated fabric listener (proposal, operator decisions pending):
   - `collab-public` gains `--collab-fabric-host/--collab-fabric-port` (suggest `100.104.39.6:9483` on bender): the collab API
     only (no MCP/A2A, web UI or operator API), no proxy and no TLS in front, `Authorization: Fabric` only (Bearer/Agent are
     refused there). Lives in the same process as the main handler: the event log has ONE writer.
   - whois goes through a host-side sidecar that exposes only `whois <ip>` on a unix socket in a shared volume; the tailscaled
     LocalAPI socket is NOT mounted into the isolated `collab-public` container.
   - Verify first that Docker's published port preserves the real source IP (userland-proxy shows the bridge gateway and Fabric
     would fail closed). Fallback: `network_mode: host` for this listener only.
6. Rollout: enable on bender+beast, register `bender-coordinator`, end-to-end drill: coordinator assigns 3 benchmark jobs by
   role to beast agents, one times out and escalates, results collected.

## 5. Test matrix (must exist before merge)
Allowed machine+active bound agent -> 200; unknown machine -> 401; allowed machine but agent bound elsewhere -> 401; agent
without `fabric` bit -> 401; external/helper agent -> 401; `X-Forwarded-For` spoof -> ignored; flag off / kill switch -> 401;
revoked agent mid-stream -> stream ends; coordinator over cap -> 429/403 with audit; coordinator cross-team -> 403; non-owner
tries to grant `coordinate` -> 403; token path unchanged (regression).

## 6. Rollback
`fabric_trust.enabled: false` or `kill_switch: true` -> every Fabric request is 401, nothing deleted, tokens and credentials
unaffected. New events are additive and ignored by older images; revoke `coordinate`/`fabric` bits before an image rollback.
An old image does not know `coordinate`: it treats a coordinator's open tasks as creator-invalid, and its sweep turns them into
permanent cancellations. Revoke `coordinate` deliberately (and let the sweep run) before rolling the image back.

## 7. Open points for review
- Which agent session names map to `HERDR_AGENT_ID` per harness (Claude/Pi/Codex/OpenCode) and how join.sh records `machine`.
- Whether `fabric` bit defaults on for agents enrolled via join.sh on an allow-listed host (proposal: yes, owner can clear it).
- tailscaled LocalAPI access for the control-plane user on the collab host (needs `tailscale set --operator` or socket group).
