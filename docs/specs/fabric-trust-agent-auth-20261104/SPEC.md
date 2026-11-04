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
      whois_cache_seconds: 30
```

## 4. Slices (each: failing test first, real server, no mocks)
1. Peer identity: whois resolver against real tailscaled in test via a local socket fixture; allow-list; unit + integration.
2. `Fabric` scheme in `authenticate()` + `still_valid()`, enroll `machine` field, `fabric` bit, audit fields, spoof negatives.
3. Agent task routes + CLI auto agent-mode.
4. `coordinate` action + coordinator-agent create-by-role + limits + audit.
5. Dashboard shows `via: fabric`/machine on events; docs (`docs/operations/fabric-trust.md`), threat-model update.
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

## 7. Open points for review
- Which agent session names map to `HERDR_AGENT_ID` per harness (Claude/Pi/Codex/OpenCode) and how join.sh records `machine`.
- Whether `fabric` bit defaults on for agents enrolled via join.sh on an allow-listed host (proposal: yes, owner can clear it).
- tailscaled LocalAPI access for the control-plane user on the collab host (needs `tailscale set --operator` or socket group).
