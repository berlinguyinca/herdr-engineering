"""Team collaboration, slice 3: the coordinator role, with limits.

Design: ``docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md`` (sections 4.6, 4.9, 4.11, 10, 10.2, 13,
14 slice 3, 15, 16). Operation and rollback: ``docs/operations/team-rooms.md``.

Everything sits behind ``control_plane.collab.coordinators.enabled`` (YAML only, absent means off, read per request, fail
closed) and also needs ``team_rooms`` and ``team_roles`` on, because a coordinator assigns by ROLE. The external-person
variant has its own second flag, ``coordinators.external.enabled``. With either off nothing changes for anybody.
"""
from __future__ import annotations

import re
from typing import Any

COORDINATORS_KEYS = frozenset({"enabled", "kill_switch", "tasks_per_hour", "max_open", "external"})
EXTERNAL_KEYS = frozenset({"enabled", "tasks_per_hour", "max_open", "max_depth", "max_items_per_request"})


def normalize_coordinators(raw: Any) -> dict:
    """Validate ``control_plane.collab.coordinators`` (YAML only). Unknown keys and wrong types raise ValueError. Everything is
    OFF until ``enabled: true`` (and, for people, ``external.enabled: true``); ``kill_switch: true`` turns both off at once.
    The external cap must be strictly LOWER than the operator-owned cap (design section 15)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.coordinators must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - COORDINATORS_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.coordinators: unknown keys: " + ", ".join(unknown))
    cfg: dict[str, Any] = {"enabled": False, "kill_switch": False, "tasks_per_hour": 20, "max_open": 20}
    ranges = {"tasks_per_hour": (2, 200), "max_open": (1, 100)}
    for key, value in raw.items():
        name = f"control_plane.collab.coordinators.{key}"
        if key in ("enabled", "kill_switch"):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif key == "external":
            continue
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer {lo}-{hi}")
        cfg[key] = value
    cfg["external"] = _normalize_external(raw.get("external"), cfg)
    return cfg


def _normalize_external(raw: Any, parent: dict) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.coordinators.external must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - EXTERNAL_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.coordinators.external: unknown keys: " + ", ".join(unknown))
    ext: dict[str, Any] = {"enabled": False, "tasks_per_hour": min(5, parent["tasks_per_hour"] - 1),
                           "max_open": min(5, parent["max_open"]), "max_depth": 2, "max_items_per_request": 10}
    ranges = {"tasks_per_hour": (1, 100), "max_open": (1, 50), "max_depth": (1, 3), "max_items_per_request": (1, 50)}
    for key, value in raw.items():
        name = f"control_plane.collab.coordinators.external.{key}"
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer {lo}-{hi}")
        ext[key] = value
    if ext["tasks_per_hour"] >= parent["tasks_per_hour"]:
        raise ValueError("control_plane.collab.coordinators.external.tasks_per_hour must be LOWER than "
                         "control_plane.collab.coordinators.tasks_per_hour")
    if ext["max_open"] > parent["max_open"]:
        raise ValueError("control_plane.collab.coordinators.external.max_open must not exceed "
                         "control_plane.collab.coordinators.max_open")
    return ext


def _err(status: int, message: str, **kw):
    from .collab import CollabError
    return CollabError(status, message, **kw)


TC_EVENTS = frozenset({"team_coordinator_set", "team_coordinator_cleared", "team_external_coordination_set", "task_requeued"})
COORDINATOR_ACTIONS = ("execute", "instruct", "post", "read")     # execute: it must accept the root task (design 7 says no)
LIMIT_KEYS = frozenset({"roles", "tasks_per_hour", "max_open", "max_depth", "max_items_per_request"})
_HOLDER = re.compile(r"(agent|user):[^\s:]{1,128}\Z")
HOUR = 3600.0


class CoordinatorsMixin:
    """State and operations for slice 3. Mixed into ``CollabService`` after the slice 1 and 2 mixins; it relies on the service's
    lock, log, rooms, agents, tasks, ``_commit``, ``_authorize``, ``_audit_denied``, ``_member``, ``_effective``, ``_ok`` and the
    slice 1 and 2 attachment and role indexes, and adds rules without changing any of theirs."""

    def _init_coordinators(self) -> None:
        self.coordinators = normalize_coordinators(None)
        self.coordinators_source = None          # optional callable -> raw YAML mapping, read per request (hot kill switch)
        self._tc_coord: dict[tuple, dict] = {}   # (room, team) -> {holder, kind, seq}
        self._tc_by_holder: dict[tuple, str] = {}   # (room, "agent:ID" | "user:ID") -> team id
        self._tc_ext: dict[tuple, dict] = {}     # (room, team) -> {enabled, limits, seq}   (the operator's per-team opt-in)
        self._tc_tasks: dict[tuple, list] = {}   # (room, team) -> ids of the tasks a coordinator made there

    # ----------------------------------------------------------------- config (hot, fail closed)
    def coordinators_cfg(self) -> dict:
        if self.coordinators_source is None:
            return self.coordinators
        try:
            return normalize_coordinators(self.coordinators_source())
        except Exception:                                      # noqa: BLE001 - any failure is OFF
            return {**normalize_coordinators(None), "enabled": False, "kill_switch": True}

    def coordinators_active(self) -> bool:
        cfg = self.coordinators_cfg()
        return bool(cfg["enabled"] and not cfg["kill_switch"] and self.team_roles_active())

    def external_coordinators_active(self) -> bool:
        return bool(self.coordinators_active() and self.coordinators_cfg()["external"]["enabled"])

    def _tc_need_on(self) -> dict:
        if not self.coordinators_active():
            raise _err(404, "not found")                       # off means the route does not exist
        return self.coordinators_cfg()

    # ----------------------------------------------------------------- the fold
    def _apply_coordinators(self, ev: dict) -> bool:
        t, p, seq = ev["event_type"], ev["payload"], ev["sequence"]
        if t not in TC_EVENTS:
            return False
        if t == "task_requeued":
            tk = self._tasks.get(p["task_id"])
            if tk is not None:
                self._tc_fold_requeue(tk, p)
            return True
        rid, tid = p["room_id"], p["team_id"]
        if t == "team_coordinator_set":
            self._tc_coord[(rid, tid)] = {"holder": p["holder"], "kind": p["kind"], "seq": seq, "set_by": p["actor_user_id"],
                                          "at": p["created_at"]}
            self._tc_by_holder[(rid, p["holder"])] = tid
            if p["kind"] == "agent":
                aid = p["holder"][6:]
                self._rooms[rid]["grants"][aid] = {"actions": list(p["actions"]), "expires_at": None, "granted_seq": seq,
                                                   "team_id": tid, "coordinator": True}
                self._agent_projects[aid].add(p["project_id"])
                self._rooms_by_agent[aid].add(rid)
        elif t == "team_coordinator_cleared":
            have = self._tc_coord.get((rid, tid))
            if have is not None and have["holder"] == p["holder"]:
                del self._tc_coord[(rid, tid)]
            self._tc_by_holder.pop((rid, p["holder"]), None)
            if p["holder"].startswith("agent:"):
                aid = p["holder"][6:]
                g = self._rooms[rid]["grants"].get(aid)
                if g is not None and g.get("coordinator") and g.get("team_id") == tid:      # only what the designation made
                    self._rooms[rid]["grants"].pop(aid, None)
                    self._rooms_by_agent[aid].discard(rid)
                    self._prune_terminal(rid, lambda k, a=aid: k == "agent:" + a)
        else:                                                    # team_external_coordination_set
            self._tc_ext[(rid, tid)] = {"enabled": bool(p["enabled"]), "limits": p.get("limits"), "seq": seq}
        return True

    def _tc_fold_created(self, tid: str, p: dict) -> None:
        """Called by the ``task_created`` fold. ``origin == "coordinator"`` is written only by the server, never from a body."""
        if p.get("origin") != "coordinator":
            return
        t = self._tasks[tid]
        t["coord"] = {"id": tid, "key": p["coordinator_key"], "team": p["coordinator_team"], "root": p.get("root_task_id")}
        self._tc_tasks.setdefault((p["room_id"], p["coordinator_team"]), []).append(tid)

    def _tc_fold_requeue(self, tk: dict, p: dict) -> None:
        """Fold of ``task_requeued``: the new holder is in the event (a replay never re-routes), the attempt rises so the old
        run is fenced out, and the operator adopts the task: whoever asked is gone, the work is the operator's to keep or cancel."""
        tid = p["task_id"]
        old_user, old_agent = tk.get("target_user"), tk.get("target")
        for idx, key in ((self._tasks_by_user, old_user), (self._tasks_by_target, old_agent)):
            if key and tid in idx.get(key, ()):
                idx[key].remove(tid)
        tk.setdefault("prior", []).append(p["from"])
        tk["attempt"] = tk.get("attempt", 1) + 1
        tk.pop("accepted_epoch", None)
        tk.update(status="pending", result=None, updated_at=p["created_at"], routed_to=p["to"], hard_expiry=p["hard_expiry_epoch"],
                  by_kind="user", by_id=p["adopt_user_id"], by_owner=p["adopt_user_id"], adopted_from=p["coordinator_key"])
        if p["to"].startswith("user:"):
            uid = p["to"][5:]
            tk.update(target=None, target_user=uid,
                      lease={"seconds": p["lease_seconds"], "start": p["at_epoch"], "expires": p["lease_expires_epoch"],
                             "fractions": list(p["reminder_fractions"]), "sent": set(), "ext": 0, "max_ext": p["max_extensions"],
                             "policy": p["no_response_policy"], "reassigned": 0})
            self._tasks_by_user.setdefault(uid, []).append(tid)
        else:
            aid = p["to"][6:]
            tk.pop("target_user", None)
            tk.pop("lease", None)
            tk["target"] = aid
            self._tasks_by_target[aid].append(tid)

    def _tc_forget_team(self, room_id: str, team_id: str) -> None:
        """A detach ends the designation and the opt-in with the attachment (called from the slice 2 forget hook)."""
        have = self._tc_coord.pop((room_id, team_id), None)
        if have is not None:
            self._tc_by_holder.pop((room_id, have["holder"]), None)
        self._tc_ext.pop((room_id, team_id), None)

    def _tc_recount(self) -> dict:
        """Designations and opt-ins that can still emit a revocation (pre-paid, see ``REVOCABLE_CREATE``)."""
        return {"teamcoord": len(self._tc_coord), "teamext": sum(1 for e in self._tc_ext.values() if e["enabled"])}

    # ----------------------------------------------------------------- who is a coordinator, right now
    def _tc_designated(self, pr, room_id: str) -> dict | None:
        """The designation this principal holds in this room, active or not. Nothing while the flag is off."""
        if not self.coordinators_active():
            return None
        team = self._tc_by_holder.get((room_id, pr.key))
        if team is None:
            return None
        return {"team_id": team, "key": pr.key, "kind": "operator" if pr.is_agent else "external", "room_id": room_id}

    def _tc_for(self, pr, room_id: str) -> dict | None:
        """The designation of a principal who may coordinate NOW: flags on, opt-in on, still a member, still allowed."""
        ident = self._tc_designated(pr, room_id)
        if ident is None:
            return None
        room = self._rooms.get(room_id)
        return ident if room is not None and self._tc_holder_active(room, ident["team_id"], ident["key"]) else None

    def _tc_holder_active(self, room: dict, team_id: str, key: str) -> bool:
        rid = room["id"]
        have = self._tc_coord.get((rid, team_id))
        if have is None or have["holder"] != key or not self.coordinators_active():
            return False
        if self._tr2_attached(rid, team_id) is None:
            return False
        if key.startswith("agent:"):
            from .collab import Principal
            a = self._agents.get(key[6:])
            if not a or a["status"] != "active" or a.get("external"):
                return False
            eff = self._effective(Principal("agent", key[6:], a["owner"]), room)
            return bool(eff and "instruct" in eff[0])
        if not self.external_coordinators_active():
            return False
        opt = self._tc_ext.get((rid, team_id))
        m = self._member(room, key[5:])
        return bool(opt and opt["enabled"] and m and m["role"] == "guest" and self._ok(key[5:]))

    def _tc_is_coordinator_agent(self, agent_id: str) -> bool:
        return any(h == "agent:" + agent_id for (_, h) in self._tc_by_holder)

    def _tc_assistants(self) -> set[str]:
        """Assistant-kind agents, derived from the log and never from a label the client sent (``harness_type`` is free text):
        every designated coordinator agent, in any room, and every agent an assistant runtime claimed."""
        out = {h for (_, h) in self._tc_by_holder if h.startswith("agent:")}
        out |= {"agent:" + r["agent_id"] for r in self._assistant_runtimes.values() if r.get("agent_id")}
        return out

    # ----------------------------------------------------------------- limits
    def _tc_limits(self, ident: dict) -> dict:
        from .collab import MAX_TASK_DEPTH
        from .collab_team_rooms import TEAM_ROLE_ACTIONS
        cfg = self.coordinators_cfg()
        if ident["kind"] == "operator":
            return {"roles": sorted(TEAM_ROLE_ACTIONS), "tasks_per_hour": cfg["tasks_per_hour"], "max_open": cfg["max_open"],
                    "max_depth": MAX_TASK_DEPTH, "max_items_per_request": None}
        ceil = cfg["external"]
        have = (self._tc_ext.get((ident["room_id"], ident["team_id"])) or {}).get("limits") or {}
        return {"roles": sorted(have.get("roles") or []),
                "tasks_per_hour": min(have.get("tasks_per_hour", ceil["tasks_per_hour"]), ceil["tasks_per_hour"]),
                "max_open": min(have.get("max_open", ceil["max_open"]), ceil["max_open"]),
                "max_depth": min(have.get("max_depth", ceil["max_depth"]), ceil["max_depth"]),
                "max_items_per_request": min(have.get("max_items_per_request", ceil["max_items_per_request"]),
                                             ceil["max_items_per_request"])}

    def _tc_usage(self, ident: dict) -> dict:
        now = self._now()
        mine = self._tc_mine(ident)
        return {"created_last_hour": sum(1 for t in mine if t["created_epoch"] >= now - HOUR),
                "open": sum(1 for t in mine if not t.get("adopted_from") and self._task_status(t) in ("pending", "accepted"))}

    def _tc_mine(self, ident: dict) -> list[dict]:
        out = []
        for tid in self._tc_tasks.get((ident["room_id"], ident["team_id"]), ()):
            t = self._tasks.get(tid)
            if t is not None and t["coord"]["key"] == ident["key"]:
                out.append(t)
        return out

    def _tc_check_caps(self, ident: dict, limits: dict, root_id: str | None, depth: int) -> None:
        """The cap is counted from the replayed tasks (not an in-memory counter), so a restart cannot reset it."""
        use = self._tc_usage(ident)
        if depth > limits["max_depth"]:
            raise _err(400, "task chain is too deep for this coordinator", code="coordinator_depth")
        if use["created_last_hour"] >= limits["tasks_per_hour"]:
            raise _err(429, "coordinator task rate limit exceeded: too many tasks in the last hour", code="coordinator_rate")
        if use["open"] >= limits["max_open"]:
            raise _err(429, "coordinator has too many open tasks", code="coordinator_open")
        cap = limits["max_items_per_request"]
        if cap is not None and root_id is not None and sum(
                1 for t in self._tc_mine(ident) if t["coord"]["root"] == root_id) >= cap:
            raise _err(429, "this request already has the most items a coordinator may make for it", code="coordinator_items")

    # ----------------------------------------------------------------- create-path hooks
    def _tc_authorize_create(self, pr, room_id: str):
        """``create_task``'s first check. A designated external coordinator is a guest by room role and has no ``task`` right, so
        they are authorised here by ``coordinate`` (read + the opt-in), or refused with 403 and an audit event under their own
        identity. Everyone else is checked exactly as before."""
        if not pr.is_agent:
            ident = self._tc_designated(pr, room_id)
            if ident is not None:
                room, first = self._authorize(pr, room_id, "read", "task.create")
                if not self._tc_holder_active(room, ident["team_id"], pr.key):
                    self._audit_denied(pr, "task.create", "coordination_revoked", room_id=room_id,
                                       project_ids=[room["project_id"]], resource_id=room_id)
                    raise _err(403, "forbidden: you may not coordinate this team now", code="coordination_revoked")
                return room, first
        return self._authorize(pr, room_id, "instruct" if pr.is_agent else "task", "task.create")

    def _tc_check_body(self, pr, room_id: str, ident: dict, limits: dict, body: dict) -> None:
        """A coordinator assigns by ROLE to its own team's holders, nothing else."""
        room = self._rooms[room_id]
        audit = dict(room_id=room_id, project_ids=[room["project_id"]], resource_id=room_id)
        if body.get("target_agent_id") is not None or body.get("target_user_id") is not None or body.get("target_role") is None:
            raise _err(400, "a coordinator assigns work by role: name target_role", code="coordinator_role_only")
        if body.get("team_id") not in (None, ident["team_id"]):
            self._audit_denied(pr, "task.create", "coordinator_other_team", **audit)
            raise _err(403, "a coordinator assigns only to its own team's role holders", code="coordinator_other_team")
        if body["target_role"] not in limits["roles"]:
            self._audit_denied(pr, "task.create", "coordinator_role_not_allowed", **audit)
            raise _err(403, "that role is not one this coordinator may assign to", code="coordinator_role_not_allowed")
        if body.get("lease_seconds") is not None or body.get("no_response_policy") is not None:
            raise _err(400, "only the room owner sets lease_seconds and no_response_policy")

    def _tc_person_parent(self, pr, room: dict, room_id: str, parent: Any) -> dict:
        """The root of an external coordinator's chain: a person task the OPERATOR made for them and they accepted."""
        pt = self._tasks.get(parent) if isinstance(parent, str) else None
        owner = (self._member(room, pt["by_id"]) or {}).get("role") == "owner" if pt and pt["by_kind"] == "user" else False
        if (not pt or pt["room_id"] != room_id or pt.get("target_user") != pr.id or self._task_status(pt) != "accepted"
                or pt["depth"] != 0 or pt["parent"] is not None or pt.get("coord") or not owner):
            raise _err(400, "coordinators work under a task the room owner gave them and they accepted: parent_task_id")
        return pt

    def _tc_chain_keys(self, creator_key: str, pt: dict | None) -> set[str]:
        keys = {creator_key}
        cur, seen = pt, set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            keys.add(f"{cur['by_kind']}:{cur['by_id']}")
            if cur.get("target"):
                keys.add("agent:" + cur["target"])
            if cur.get("target_user"):
                keys.add("user:" + cur["target_user"])
            if cur.get("coord"):
                keys.add(cur["coord"]["key"])
            cur = self._tasks.get(cur["parent"]) if cur.get("parent") else None
        return keys

    def _tc_loop_keys(self, creator_key: str, pt: dict | None, candidates, no_assistants: bool = False) -> set[str]:
        """Which of ``candidates`` would close a loop if handed the next task of this chain: anyone who already handed out or
        held a task up the chain (A -> B -> C -> A included), and any assistant-kind agent once an assistant is anywhere in the
        chain. The existing guards cover self-tasking and handing a task straight back; they do not cover either of these."""
        if not self.coordinators_active():
            return set()
        chain = self._tc_chain_keys(creator_key, pt)
        assistants = self._tc_assistants()
        tainted = no_assistants or bool(chain & assistants)
        return {k for k in candidates if k in chain or (tainted and k in assistants)}

    def _tc_loop_check(self, pr, pt: dict | None, target_key: str) -> None:
        if self._tc_loop_keys(pr.key, pt, [target_key]):
            raise _err(400, "this would send work back around a chain of agents, or from one assistant to another",
                       code="coordination_loop")

    def _tc_stamp(self, pr, room_id: str):
        """Extra audit fields for a coordinator principal: every event it causes says so."""
        ident = self._tc_designated(pr, room_id) if room_id else None
        return {"origin": "coordinator", "coordinator_team": ident["team_id"]} if ident else {}

    # ----------------------------------------------------------------- owner operations
    def _tc_team(self, room_id: str, team_id: Any) -> str:
        from .collab_team_rooms import TEAM_ID
        if not isinstance(team_id, str) or not TEAM_ID.match(team_id) or self._tr2_attached(room_id, team_id) is None:
            raise _err(404, "not found")
        return team_id

    def set_team_coordinator(self, pr, room_id: str, team_id: str, body: dict) -> dict:
        self._tc_need_on()
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.coordinator.set")
            self._tc_team(room_id, team_id)
            if set(body) - {"holder"}:
                raise _err(400, "unknown field")
            holder = body.get("holder")
            if not isinstance(holder, str) or not _HOLDER.match(holder):
                raise _err(400, "holder must be 'agent:<id>' (the operator's own agent) or 'user:<id>' (an external person, "
                                "only after the team's opt-in)")
            have = self._tc_coord.get((room_id, team_id))
            if have is not None:
                if have["holder"] == holder:
                    return {**self._tc_view_row(room_id, team_id), "replayed": True}
                raise _err(409, "this team already has a coordinator; clear it first", code="coordinator_set")
            elsewhere = self._tc_by_holder.get((room_id, holder))
            if elsewhere is not None:
                raise _err(409, "that principal already coordinates another team in this room", code="coordinator_elsewhere")
            kind = "agent" if holder.startswith("agent:") else "person"
            extra: dict[str, Any] = {}
            if kind == "agent":
                aid = holder[6:]
                a = self._agents.get(aid)
                if not a or a["status"] != "active" or a["owner"] != pr.id:
                    raise _err(404, "no such agent of yours")                  # one answer for every miss
                if a.get("external"):
                    raise _err(400, "a foreign (external) agent cannot coordinate a team")
                if self._helper_by_agent.get(aid) is not None or aid in self._helper_minted:
                    raise _err(400, "a helper cannot coordinate a team")
                if aid in room["grants"]:
                    raise _err(409, f"agent {a['name']} already holds access to this room; remove it first")
                self._name_free(room, aid, a["name"])
                extra["actions"] = list(COORDINATOR_ACTIONS)
            else:
                uid = holder[5:]
                if uid == pr.id:
                    raise _err(400, "the room owner is not a coordinator: the operator's assistant is (an operator-owned agent)")
                if not self.external_coordinators_active():
                    raise _err(409, "external coordination is off on this service", code="external_coordination_off")
                opt = self._tc_ext.get((room_id, team_id))
                if not opt or not opt["enabled"]:
                    raise _err(409, "turn on external coordination for this team first", code="external_opt_in_required")
                m = self._member(room, uid)
                if not m or not self._ok(uid) or m["role"] != "guest":
                    raise _err(400, "an external coordinator must be a current guest of this room (a member with a wider role "
                                    "already holds more than coordinate allows)")
            self._throttle("team-coordinator:" + pr.key, 30, 600.0, "team coordinator")
            self._commit("team_coordinator_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, holder=holder, kind=kind, **extra,
                visibility_scope="room", action="team.coordinator.set", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=team_id))
            return {**self._tc_view_row(room_id, team_id), "replayed": False}

    def clear_team_coordinator(self, pr, room_id: str, team_id: str) -> dict:
        """Owner only; reachable with the flags off (a kill switch never blocks a revocation). It cancels an operator-owned
        coordinator's open work, or requeues an external coordinator's."""
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.coordinator.clear")
            have = self._tc_coord.get((room_id, team_id)) if isinstance(team_id, str) else None
            if have is None:
                return {"room_id": room_id, "team_id": team_id, "holder": None, "replayed": True, "cancelled": [], "requeued": []}
            holder = have["holder"]
            self._commit("team_coordinator_cleared", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, holder=holder, reason="owner",
                visibility_scope="room", action="team.coordinator.clear", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=team_id))
            out = self._tc_reconcile_team(room_id, team_id)
            return {"room_id": room_id, "team_id": team_id, "holder": holder, "replayed": False, **out}

    def _tc_limits_body(self, raw: Any) -> dict:
        from .collab_team_rooms import TEAM_ROLE_ACTIONS
        ceil = self.coordinators_cfg()["external"]
        if not isinstance(raw, dict) or set(raw) - LIMIT_KEYS:
            raise _err(400, "limits may only use: " + ", ".join(sorted(LIMIT_KEYS)))
        roles = raw.get("roles")
        if (not isinstance(roles, list) or not roles or len(roles) > len(TEAM_ROLE_ACTIONS)
                or not all(isinstance(r, str) and r in TEAM_ROLE_ACTIONS for r in roles)):
            raise _err(400, "limits.roles must list the team roles this person may assign to: " + ", ".join(sorted(TEAM_ROLE_ACTIONS)))
        out: dict[str, Any] = {"roles": sorted(set(roles))}
        for key, hi in (("tasks_per_hour", ceil["tasks_per_hour"]), ("max_open", ceil["max_open"]),
                        ("max_depth", ceil["max_depth"]), ("max_items_per_request", ceil["max_items_per_request"])):
            if key in raw:
                v = raw[key]
                if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= hi:
                    raise _err(400, f"limits.{key} must be an integer 1-{hi} (the service-wide ceiling)")
                out[key] = v
        return out

    def set_external_coordination(self, pr, room_id: str, team_id: str, body: dict) -> dict:
        """The per-team opt-in. Owner user only, off by default. Turning it ON needs both flags; turning it OFF is a revocation and
        stays reachable with the flags off: it ends the designation and requeues the person's open assignments."""
        if body.get("enabled") is True and not self.external_coordinators_active():
            raise _err(404, "not found")                       # off means the route does not exist (the OFF direction stays)
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.external_coordination.set")
            if set(body) - {"enabled", "limits"}:
                raise _err(400, "unknown field")
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise _err(400, "enabled must be true or false")
            have = self._tc_ext.get((room_id, team_id)) if isinstance(team_id, str) else None
            if enabled:
                if not self.external_coordinators_active():
                    raise _err(404, "not found")
                self._tc_team(room_id, team_id)
                limits = self._tc_limits_body(body.get("limits"))
                if have is not None and have["enabled"] and have["limits"] == limits:
                    return {**self._tc_ext_view(room_id, team_id), "replayed": True}
                self._throttle("team-ext-coordination:" + pr.key, 30, 600.0, "external coordination")
                self._commit("team_external_coordination_set", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, enabled=True, limits=limits,
                    visibility_scope="room", action="team.external_coordination.set", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=team_id))
                return {**self._tc_ext_view(room_id, team_id), "replayed": False}
            if body.get("limits") is not None:
                raise _err(400, "limits only go with enabled true")
            if have is None or not have["enabled"]:
                return {**self._tc_ext_view(room_id, team_id), "replayed": True, "cancelled": [], "requeued": []}
            self._commit("team_external_coordination_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, enabled=False, limits=None,
                visibility_scope="room", action="team.external_coordination.set", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=team_id))
            coord = self._tc_coord.get((room_id, team_id))
            if coord is not None and coord["kind"] == "person":      # the designation cannot outlive its opt-in
                self._commit("team_coordinator_cleared", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, holder=coord["holder"],
                    reason="external_coordination_off", visibility_scope="room", action="team.coordinator.clear",
                    outcome="allowed", project_ids=[room["project_id"]], resource_id=team_id))
            out = self._tc_reconcile_team(room_id, team_id)
            return {**self._tc_ext_view(room_id, team_id), "replayed": False, **out}

    # ----------------------------------------------------------------- views
    def _tc_view_row(self, room_id: str, team_id: str) -> dict:
        have = self._tc_coord.get((room_id, team_id))
        room = self._rooms[room_id]
        out: dict[str, Any] = {"room_id": room_id, "team_id": team_id, "holder": have["holder"] if have else None,
                               "kind": ({"agent": "operator", "person": "external"}[have["kind"]] if have else None),
                               "active": bool(have and self._tc_holder_active(room, team_id, have["holder"])),
                               "external": self._tc_ext_row(room_id, team_id)}
        if have:
            ident = {"team_id": team_id, "key": have["holder"], "room_id": room_id, "kind": out["kind"]}
            out.update(limits=self._tc_limits(ident), usage=self._tc_usage(ident))
        return out

    def _tc_ext_row(self, room_id: str, team_id: str) -> dict:
        opt = self._tc_ext.get((room_id, team_id))
        return {"enabled": bool(opt and opt["enabled"]), "limits": opt["limits"] if opt and opt["enabled"] else None,
                "service_enabled": self.external_coordinators_active()}

    def _tc_ext_view(self, room_id: str, team_id: str) -> dict:
        return {"room_id": room_id, "team_id": team_id, **self._tc_ext_row(room_id, team_id)}

    def view_team_coordinator(self, pr, room_id: str, team_id: str) -> dict:
        self._tc_need_on()
        with self._lock:
            self._tr_owner(pr, room_id, "team.coordinator.view")
            self._tc_team(room_id, team_id)
            return self._tc_view_row(room_id, team_id)

    def coordination_view(self, pr, room_id: str) -> dict:
        """What a coordinator may see of its own work: its limits, its usage and the status of the tasks it made. An external
        person gets status only (no results: tracking is not a way to read what the team produced)."""
        self._tc_need_on()
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "coordination.view")
            ident = self._tc_for(pr, room_id)
            if ident is None:
                raise _err(404, "not found")
            ext_kind = ident["kind"] == "external"
            rows = []
            for t in sorted(self._tc_mine(ident), key=lambda x: x["seq"], reverse=True)[:200]:
                row = {"task_id": t["coord"]["id"], "status": self._task_status(t), "role": t.get("role"), "attempt": t.get("attempt", 1),
                       "parent_task_id": t["parent"], "depth": t["depth"], "created_at": t["created_at"],
                       "updated_at": t["updated_at"], "text": t["text"], "adopted": bool(t.get("adopted_from"))}
                if not ext_kind:
                    row["result"] = t["result"]
                rows.append(row)
            return {"room_id": room_id, "team_id": ident["team_id"], "kind": ident["kind"], "limits": self._tc_limits(ident),
                    "usage": self._tc_usage(ident), "tasks": rows, "untrusted": True}

    def _tc_blind_get(self, pr, room: dict, task: dict) -> bool:
        """A guest (an external coordinator is one) cannot read a task by id while the flag is on, apart from their own person
        task: ``list_tasks`` hides the list from guests and the id must not be a way around it."""
        return bool(self.coordinators_active() and self._tr_blinds_tasks(pr, room) and task.get("target_user") != pr.id)

    # ----------------------------------------------------------------- revocation: cancel or requeue, derived from state
    def _tc_system(self, t: dict, room: dict, tid: str) -> dict:
        return dict(room_id=t["room_id"], project_id=room["project_id"], task_id=tid, visibility_scope="room", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=tid, actor_user_id=None, actor_agent_id=None,
                    actor_kind="system")

    def _tc_cancel(self, tid: str, t: dict, room: dict, reason: str, out: dict) -> None:
        self._commit("task_updated", {**self._tc_system(t, room, tid), "status": "cancelled", "result": None,
                                      "target_agent_id": t["target"], **self._tr2_event_fields(t), "action": "task.cancel",
                                      "reason": reason})
        out["cancelled"].append(tid)

    def _tc_requeue(self, tid: str, t: dict, room: dict, out: dict) -> None:
        """Return one open task of a revoked external coordinator to its role queue under the operator, with a higher attempt."""
        from .collab import TASK_TTL
        cfg = self.team_roles_cfg()
        now = self._now()
        exclude: set[str] = set()
        if t.get("reviews") in self._tasks:
            exclude |= set(self._tr2_disqualified(self._tasks[t["reviews"]]))
        parent = self._tasks.get(t["parent"]) if t.get("parent") else None
        root = f"user:{t['root']}"
        exclude |= self._tc_loop_keys(root, parent, [k for k in self._tr2_holder_keys(t["room_id"], t["coord"]["team"], t["role"])])
        cands = self._tr2_candidates(t["room_id"], room, t["coord"]["team"], t["role"], exclude=exclude) if t.get("role") else []
        keys = [k for _, k in cands]
        current = t.get("routed_to")
        pick = current if current in keys else (keys[0] if keys else None)
        if pick is None:                                  # nobody can take it: cancel rather than leave it dead
            self._tc_cancel(tid, t, room, "coordinator_revoked_no_holder", out)
            return
        extra: dict[str, Any] = dict(lease_seconds=0, lease_expires_epoch=0, reminder_fractions=[], no_response_policy="escalate",
                                     max_extensions=0, hard_expiry_epoch=now + TASK_TTL)
        if pick.startswith("user:"):
            seconds = cfg["person_lease_seconds"]
            extra.update(lease_seconds=seconds, lease_expires_epoch=now + seconds,
                         reminder_fractions=list(cfg["reminder_fractions"]), max_extensions=cfg["max_lease_extensions"],
                         no_response_policy=self._tr2_team_policy(t["room_id"], t["coord"]["team"]),
                         hard_expiry_epoch=now + max(cfg["hard_expiry_seconds"], seconds), target_user_id=pick[5:])
        self._commit("task_requeued", {**self._tc_system(t, room, tid), "action": "task.requeue.revoked", "from": current,
                                       "to": pick, "reason": "coordinator_revoked", "coordinator_key": t["coord"]["key"],
                                       "adopt_user_id": t["root"], "at_epoch": now, **extra})
        out["requeued"].append(tid)

    def _tc_reconcile_team(self, room_id: str, team_id: str) -> dict:
        """Bring the open work of every coordinator of this team in line with who may coordinate NOW. Derived from state, so it is
        idempotent (a repeat finds nothing to do), survives a restart and runs the same whether the cause was the opt-in, a
        removal, a detach or the identity provider dropping the person's group."""
        out: dict[str, list] = {"cancelled": [], "requeued": []}
        room = self._rooms.get(room_id)
        if room is None:
            return out
        from .collab import CollabError
        by_key: dict[str, list] = {}
        for tid in list(self._tc_tasks.get((room_id, team_id), ())):
            t = self._tasks.get(tid)
            if t is not None and not t.get("adopted_from"):
                by_key.setdefault(t["coord"]["key"], []).append(tid)
        try:
            for key, tids in by_key.items():
                if self._tc_holder_active(room, team_id, key):
                    continue
                if key.startswith("user:"):
                    for tid in tids:
                        t = self._tasks.get(tid)
                        if t is not None and self._task_status(t) in ("pending", "accepted"):
                            self._tc_requeue(tid, t, room, out)
                else:
                    self._tc_cancel_chain(room, tids, out)
        except CollabError:
            pass                                          # log full or unavailable: authority is already off; the sweep retries
        return out

    def _tc_cancel_chain(self, room: dict, own: list, out: dict) -> None:
        """An operator-owned coordinator was revoked: cancel its open tasks and everything delegated beneath them."""
        gone = set(own)
        for tid, t in sorted(self._tasks.items(), key=lambda kv: kv[1]["seq"]):
            if t["room_id"] == room["id"] and t.get("parent") in gone:
                gone.add(tid)
        for tid in sorted(gone, key=lambda i: self._tasks[i]["seq"]):
            t = self._tasks.get(tid)
            if t is not None and self._task_status(t) in ("pending", "accepted", "blocked"):
                self._tc_cancel(tid, t, room, "coordinator_revoked", out)

    def _tc_reconcile_room(self, room_id: str) -> None:
        for (rid, team_id) in list(self._tc_tasks):
            if rid == room_id:
                self._tc_reconcile_team(rid, team_id)

    def sweep_coordinators(self) -> dict:
        """Run by the control plane's maintenance thread next to ``sweep_team_roles``. The identity provider can drop a person's
        group without any request from them, so the server must notice by itself. A no-op while the flag is off."""
        if not self.coordinators_active():
            return {"active": False}
        if self.degraded:
            return {"active": True, "degraded": True}
        out = {"active": True, "cancelled": 0, "requeued": 0}
        with self._lock:
            for (rid, team_id) in list(self._tc_tasks):
                res = self._tc_reconcile_team(rid, team_id)
                out["cancelled"] += len(res["cancelled"])
                out["requeued"] += len(res["requeued"])
        return out
