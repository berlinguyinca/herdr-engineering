"""Team collaboration, slice 2: roles, routing by role, people as role holders, per-task leases and independent review.

Design: ``docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md`` (sections 4.2-4.5, 7, 8, 14 slice 2, 15).
Operation and rollback: ``docs/operations/team-rooms.md``.

Authority lives in the collab log, decided where ``_authorize`` and the service lock decide everything else. A team's role
holders are kept per (room, team): an agent holds a role through the slice-1 attach, a PERSON holds it through
``team_role_assigned`` (owner user only). Membership of the room is never touched and a room member is never implicitly a
holder.

* **Routing.** ``create_task`` accepts ``target_role`` (and ``team_id`` when more than one attached team holds the role)
  instead of ``target_agent_id``. The holder with the fewest open tasks wins; a tie goes to the smallest principal key
  (``agent:<id>`` before ``user:<id>``). The resolved holder is written into the ``task_created`` event, so a replay never
  re-routes. No eligible holder is a 409 ``no_role_holder`` and nothing is written: the request never falls through to an
  agent that is not in the role. An agent holder is eligible only if it may ``execute`` here, which the slice-1 role actions
  give to ``implementer`` and ``tester`` and not to ``issue_writer`` or ``reviewer`` (they are routable when a person holds them).
* **People.** A task for a person is ``target_agent_id: None`` plus ``target_user_id``; it is in no agent inbox. The person
  reads it in ``my-tasks`` and answers with ``respond`` (accepted, done, declined). Reading is limited to the assignee, the
  room owner and the task's creator in the task list, the room feed and ``my-tasks``.
* **The clock.** A person never polls, so ``sweep_team_roles`` (run by a daemon thread of the control plane, and callable by a
  test after it moves the clock) evaluates every open person task as a pure function of the replayed state and ``now``. Each
  outcome is an event that changes the state the next sweep reads, so a repeat commits nothing and a restart loses nothing:
  ``task_reminded`` (once per reminder fraction, before expiry only), ``task_lease_extended`` (policy ``wait``),
  ``task_reassigned`` (policy ``reassign``, once) and ``task_escalated`` (the task becomes ``blocked`` and shows in the
  owner's attention). No migration: the deadline, the reminders sent and the policy are all in the log.
* **Independent review.** With the flag on, a review is checked against principal keys (``agent:<id>``, ``user:<id>``): not
  the implementer, not whoever handed the work out (the creator and the creators up the parent chain), not anyone who held
  it before a reassignment, and nobody in a configured reviewer group of one of them.

Everything sits behind ``control_plane.collab.team_roles.enabled`` (YAML only, absent means off, read per request, fail
closed) and also needs ``team_rooms`` on. With it off ``create_task`` rejects the new fields as unknown and the routes are 404.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import ids
from .collab_team_rooms import TEAM_ID, TEAM_ROLE_ACTIONS

ROLE_KIND = {"issue_writer": "planner", "implementer": "implementer", "tester": "implementer", "reviewer": "reviewer"}
POLICIES = ("reassign", "escalate", "wait")
TR2_EVENTS = frozenset({"team_role_assigned", "team_role_cleared", "team_role_policy_set", "room_member_visibility_set",
                        "task_reminded", "task_reassigned", "task_escalated", "task_lease_extended"})
TEAM_ROLES_KEYS = frozenset({"enabled", "kill_switch", "person_lease_seconds", "hard_expiry_seconds", "max_lease_extensions",
                             "reminder_fractions", "default_no_response_policy", "reviewer_groups", "sweep_interval_seconds"})
ROLE_FIELDS = frozenset({"target_role", "team_id", "target_user_id", "lease_seconds", "no_response_policy"})
MAX_LEASE = 14 * 86400                 # design section 8: ceiling for a person's lease
MIN_LEASE = 60
MAX_PERSON_HOLDERS = 20
MAX_OPEN_PER_PERSON = 20
_USER_KEY = re.compile(r"user:[^\s:]{1,128}\Z")
_TASK_FIELDS = frozenset({"target_agent_id", "text", "parent_task_id", "idempotency_key", "reviews_task_id"})


def normalize_team_roles(raw: Any) -> dict:
    """Validate ``control_plane.collab.team_roles`` (YAML only). Unknown keys and wrong types raise ValueError. Everything is
    OFF until ``enabled: true``; ``kill_switch: true`` turns the whole feature off again at once."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.team_roles must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - TEAM_ROLES_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.team_roles: unknown keys: " + ", ".join(unknown))
    cfg: dict[str, Any] = {"enabled": False, "kill_switch": False, "person_lease_seconds": 3 * 86400,
                           "hard_expiry_seconds": 30 * 86400, "max_lease_extensions": 3, "reminder_fractions": [0.5, 0.9],
                           "default_no_response_policy": "escalate", "reviewer_groups": [], "sweep_interval_seconds": 30}
    ranges = {"person_lease_seconds": (MIN_LEASE, MAX_LEASE), "hard_expiry_seconds": (MIN_LEASE, 60 * 86400),
              "max_lease_extensions": (0, 10), "sweep_interval_seconds": (5, 3600)}
    for key, value in raw.items():
        name = f"control_plane.collab.team_roles.{key}"
        if key in ("enabled", "kill_switch"):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif key == "default_no_response_policy":
            if value not in POLICIES:
                raise ValueError(f"{name} must be one of: " + ", ".join(POLICIES))
        elif key == "reminder_fractions":
            if (not isinstance(value, list) or not 1 <= len(value) <= 4
                    or not all(isinstance(f, (int, float)) and not isinstance(f, bool) and 0 < f < 1 for f in value)
                    or sorted(set(value)) != list(value)):
                raise ValueError(f"{name} must be 1-4 increasing numbers between 0 and 1 (exclusive)")
            value = [float(f) for f in value]
        elif key == "reviewer_groups":
            if (not isinstance(value, list) or len(value) > 50
                    or not all(isinstance(g, list) and 2 <= len(g) <= 20
                               and all(isinstance(k, str) and re.match(r"(agent|user):[^\s]{1,128}\Z", k) for k in g)
                               for g in value)):
                raise ValueError(f"{name} must be a list of groups, each 2-20 principal keys like 'user:alice' or 'agent:ID'")
            value = [sorted(set(g)) for g in value]
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer {lo}-{hi}")
        cfg[key] = value
    return cfg


def _err(status: int, message: str, **kw):
    from .collab import CollabError
    return CollabError(status, message, **kw)


class TeamRolesMixin:
    """State and operations for slice 2. Mixed into ``CollabService`` next to ``TeamRoomsMixin``; it relies on the service's
    lock, log, rooms, agents, tasks, ``_commit``, ``_authorize``, ``_audit_denied``, ``_member``, ``_effective``, ``_throttle``,
    ``_task_text`` and the slice-1 attachment index, and changes none of their rules."""

    def _init_team_roles(self) -> None:
        self.team_roles = normalize_team_roles(None)
        self.team_roles_source = None            # optional callable -> raw YAML mapping, read per request (hot kill switch)
        self._tr2_people: dict[tuple, dict[str, list[str]]] = {}      # (room, team) -> role -> person ids, in assign order
        self._tr2_policy: dict[tuple, str] = {}                       # (room, team) -> no-response policy
        self._tr2_visible: dict[str, bool] = {}                       # room -> members may see the task list
        self._tasks_by_user: dict[str, list[str]] = {}                # person id -> task ids

    # ----------------------------------------------------------------- config (hot, fail closed)
    def team_roles_cfg(self) -> dict:
        if self.team_roles_source is None:
            return self.team_roles
        try:
            return normalize_team_roles(self.team_roles_source())
        except Exception:                                      # noqa: BLE001 - any failure is OFF
            return {**normalize_team_roles(None), "enabled": False, "kill_switch": True}

    def team_roles_active(self) -> bool:
        cfg = self.team_roles_cfg()
        return bool(cfg["enabled"] and not cfg["kill_switch"] and self.team_rooms_active())

    def _tr2_need_on(self) -> dict:
        if not self.team_roles_active():
            raise _err(404, "not found")                       # off means the route does not exist
        return self.team_roles_cfg()

    # ----------------------------------------------------------------- the fold
    def _apply_team_roles(self, ev: dict) -> bool:
        t, p, seq = ev["event_type"], ev["payload"], ev["sequence"]
        if t not in TR2_EVENTS:
            return False
        if t == "team_role_assigned":
            holders = self._tr2_people.setdefault((p["room_id"], p["team_id"]), {}).setdefault(p["role"], [])
            uid = p["holder"][5:]
            if uid not in holders:
                holders.append(uid)
        elif t == "team_role_cleared":
            roles = self._tr2_people.get((p["room_id"], p["team_id"]), {})
            if p["holder"][5:] in roles.get(p["role"], []):
                roles[p["role"]].remove(p["holder"][5:])
        elif t == "team_role_policy_set":
            self._tr2_policy[(p["room_id"], p["team_id"])] = p["policy"]
        elif t == "room_member_visibility_set":
            self._tr2_visible[p["room_id"]] = bool(p["enabled"])
        else:
            tk = self._tasks.get(p["task_id"])
            lease = (tk or {}).get("lease")
            if tk is None:
                return True
            if t == "task_reminded" and lease is not None:
                lease["sent"].add(p["which"])
            elif t == "task_lease_extended" and lease is not None:
                lease.update(start=lease["expires"], expires=p["new_expires_epoch"], ext=p["extension"], sent=set())
            elif t == "task_escalated":
                tk.update(status="blocked", updated_at=p["created_at"], escalated_reason=p["reason"])
            elif t == "task_reassigned":
                self._tr2_move(tk, p)
        return True

    def _tr2_fold_created(self, tid: str, p: dict) -> None:
        """Called by the ``task_created`` fold: role and person fields. Nothing is derived from the clock here."""
        t = self._tasks[tid]
        if p.get("target_role"):
            t.update(role=p["target_role"], team_id=p.get("team_id"), routed_to=p.get("routed_to"))
        if p.get("idempotency_key") and p.get("sender_key") and (p.get("target_role") or p.get("target_kind") == "user"):
            # rebuilt on replay, so a retried request after a restart is still the same request
            self._idem[(p["sender_key"], p["room_id"], "task:" + p["idempotency_key"])] = (p["fingerprint"], tid)
        if p.get("target_kind") == "user":
            t.update(target_user=p["target_user_id"], hard_expiry=p["hard_expiry_epoch"],
                     lease={"seconds": p["lease_seconds"], "start": p["lease_start_epoch"], "expires": p["lease_expires_epoch"],
                            "fractions": list(p["reminder_fractions"]), "sent": set(), "ext": 0,
                            "max_ext": p["max_extensions"], "policy": p["no_response_policy"], "reassigned": 0})
            self._tasks_by_user.setdefault(p["target_user_id"], []).append(tid)

    def _tr2_move(self, tk: dict, p: dict) -> None:
        """Fold of ``task_reassigned``: the resolved new holder is in the event, never recomputed."""
        tid = p["task_id"]
        old_user, old_agent = tk.get("target_user"), tk.get("target")
        tk.setdefault("prior", []).append(p["from"])
        if old_user:
            idx = self._tasks_by_user.get(old_user)
            if idx and tid in idx:
                idx.remove(tid)
        elif old_agent:
            idx = self._tasks_by_target.get(old_agent)
            if idx and tid in idx:
                idx.remove(tid)
        tk["routed_to"] = p["to"]
        if p["to"].startswith("user:"):
            uid = p["to"][5:]
            lease = tk["lease"]
            lease.update(start=p["at_epoch"], expires=p["new_expires_epoch"], sent=set(), ext=0,
                         reassigned=lease["reassigned"] + 1)
            tk.update(target=None, target_user=uid, status="pending")
            self._tasks_by_user.setdefault(uid, []).append(tid)
        else:
            aid = p["to"][6:]
            tk.pop("target_user", None)
            tk.pop("lease", None)
            tk.update(target=aid, status="pending")
            self._tasks_by_target[aid].append(tid)

    def _tr2_recount(self) -> dict:
        return {"teamrole": sum(len(h) for roles in self._tr2_people.values() for h in roles.values())}

    def _tr2_forget_team(self, room_id: str, team_id: str) -> None:
        """A detach takes the team's people out of their roles with it (it removes exactly what the team brought)."""
        self._tr2_people.pop((room_id, team_id), None)

    # ----------------------------------------------------------------- views of who holds what
    def _tr2_attached(self, room_id: str, team_id: Any):
        att = self._tr_attach.get((room_id, team_id)) if isinstance(team_id, str) else None
        return att if att is not None and att["state"] == "attached" else None

    def _tr2_holder_keys(self, room_id: str, team_id: str, role: str) -> list[str]:
        att = self._tr2_attached(room_id, team_id)
        if att is None:
            return []
        keys = ["agent:" + aid for aid, r in att["agents"].items() if r == role]
        keys += ["user:" + u for u in self._tr2_people.get((room_id, team_id), {}).get(role, ())]
        return sorted(keys)

    def _tr2_open_load(self, key: str) -> int:
        idx = self._tasks_by_user.get(key[5:], ()) if key.startswith("user:") else self._tasks_by_target.get(key[6:], ())
        return sum(1 for tid in idx if self._task_status(self._tasks[tid]) in ("pending", "accepted"))

    def _tr2_can_take(self, room: dict, key: str) -> bool:
        """May this holder be handed a task right now? A person must still be a member who may use the network; an agent must
        be active, granted here and hold ``execute``. Anything else is simply not a candidate."""
        from .collab import Principal
        if key.startswith("user:"):
            uid = key[5:]
            return bool(self._member(room, uid) and self._ok(uid))
        a = self._agents.get(key[6:])
        if not a or a["status"] != "active":
            return False
        eff = self._effective(Principal("agent", key[6:], a["owner"]), room)
        return bool(eff and "execute" in eff[0])

    def team_role_view(self, pr, room_id: str, team_id: str) -> dict:
        self._tr2_need_on()
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.role.list")
            if self._tr2_attached(room_id, team_id) is None:
                raise _err(404, "not found")
            roles = []
            for role in sorted(TEAM_ROLE_ACTIONS):
                holders = []
                for key in self._tr2_holder_keys(room_id, team_id, role):
                    if key.startswith("user:"):
                        m = self._member(room, key[5:])
                        holders.append({"holder": key, "type": "person", "member_role": m["role"] if m else None,
                                        "can_take_tasks": self._tr2_can_take(room, key)})
                    else:
                        holders.append({"holder": key, "type": "agent", "name": (self._agents.get(key[6:]) or {}).get("name"),
                                        "can_take_tasks": self._tr2_can_take(room, key)})
                roles.append({"role": role, "kind": ROLE_KIND[role], "holders": holders})
            return {"room_id": room_id, "team_id": team_id, "roles": roles,
                    "no_response_policy": self._tr2_team_policy(room_id, team_id)}

    def _tr2_team_policy(self, room_id: str, team_id: Any) -> str:
        return self._tr2_policy.get((room_id, team_id)) or self.team_roles_cfg()["default_no_response_policy"]

    # ----------------------------------------------------------------- owner operations
    def _tr2_holder(self, room: dict, body: dict) -> tuple[str, str, str]:
        if set(body) - {"role", "holder"}:
            raise _err(400, "unknown field")
        role, holder = body.get("role"), body.get("holder")
        if not isinstance(role, str) or role not in TEAM_ROLE_ACTIONS:
            raise _err(400, "role must be one of: " + ", ".join(sorted(TEAM_ROLE_ACTIONS))
                       + (" (a coordinator is a later slice)" if role == "coordinator" else ""))
        if not isinstance(holder, str) or not _USER_KEY.match(holder):
            raise _err(400, "holder must be 'user:<id>': an agent joins a role by being attached to the team")
        return role, holder, ROLE_KIND[role]

    def assign_team_role(self, pr, room_id: str, team_id: str, body: dict) -> dict:
        cfg = self._tr2_need_on()
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.role.assign")
            if self._tr2_attached(room_id, team_id) is None:
                raise _err(404, "not found")
            role, holder, kind = self._tr2_holder(room, body)
            uid = holder[5:]
            if not self._member(room, uid) or not self._ok(uid):
                raise _err(400, "that person is not a member of this room")
            have = self._tr2_people.get((room_id, team_id), {})
            if uid in have.get(role, ()):
                return {"room_id": room_id, "team_id": team_id, "role": role, "holder": holder, "kind": kind, "replayed": True}
            if sum(len(h) for h in have.values()) >= MAX_PERSON_HOLDERS:
                raise _err(429, f"a team holds at most {MAX_PERSON_HOLDERS} people in roles in one room")
            self._throttle("team-role:" + pr.key, 60, 600.0, "team role")
            self._commit("team_role_assigned", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, role=role, holder=holder, kind=kind,
                visibility_scope="room", action="team.role.assign", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=team_id))
            _ = cfg
            return {"room_id": room_id, "team_id": team_id, "role": role, "holder": holder, "kind": kind, "replayed": False}

    def clear_team_role(self, pr, room_id: str, team_id: str, body: dict) -> dict:
        """Owner only; stays reachable with the flag off (taking a person out of a role is a revocation)."""
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.role.clear")
            if not isinstance(team_id, str) or not TEAM_ID.match(team_id):
                raise _err(404, "not found")
            role, holder, kind = self._tr2_holder(room, body)
            if holder[5:] not in self._tr2_people.get((room_id, team_id), {}).get(role, ()):
                return {"room_id": room_id, "team_id": team_id, "role": role, "holder": holder, "kind": kind, "replayed": True}
            self._commit("team_role_cleared", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, role=role, holder=holder, kind=kind,
                visibility_scope="room", action="team.role.clear", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=team_id))
            return {"room_id": room_id, "team_id": team_id, "role": role, "holder": holder, "kind": kind, "replayed": False}

    def set_team_policy(self, pr, room_id: str, team_id: str, body: dict) -> dict:
        self._tr2_need_on()
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.policy.set")
            if self._tr2_attached(room_id, team_id) is None:
                raise _err(404, "not found")
            if set(body) - {"no_response_policy"}:
                raise _err(400, "unknown field")
            policy = body.get("no_response_policy")
            if not isinstance(policy, str) or policy not in POLICIES:
                raise _err(400, "no_response_policy must be one of: " + ", ".join(POLICIES))
            if self._tr2_policy.get((room_id, team_id)) == policy:
                return {"room_id": room_id, "team_id": team_id, "no_response_policy": policy, "replayed": True}
            self._throttle("team-policy:" + pr.key, 30, 600.0, "team policy")
            self._commit("team_role_policy_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, policy=policy,
                visibility_scope="room", action="team.policy.set", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=team_id))
            return {"room_id": room_id, "team_id": team_id, "no_response_policy": policy, "replayed": False}

    def get_member_visibility(self, pr, room_id: str) -> dict:
        self._tr2_need_on()
        if pr.is_agent:
            raise _err(404, "not found")
        with self._lock:
            self._authorize(pr, room_id, "read", "member_visibility.read")
            return {"room_id": room_id, "enabled": self._tr2_visible.get(room_id, False)}

    def set_member_visibility(self, pr, room_id: str, body: dict) -> dict:
        self._tr2_need_on()
        with self._lock:
            room = self._tr_owner(pr, room_id, "member_visibility.set")
            if set(body) - {"enabled"} or not isinstance(body.get("enabled"), bool):
                raise _err(400, "enabled must be true or false")
            if self._tr2_visible.get(room_id, False) == body["enabled"]:
                return {"room_id": room_id, "enabled": body["enabled"], "replayed": True}
            self._throttle("member-visibility:" + pr.key, 30, 600.0, "member visibility")
            self._commit("room_member_visibility_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, enabled=body["enabled"], visibility_scope="room",
                action="member_visibility.set", outcome="allowed", project_ids=[room["project_id"]], resource_id=room_id))
            return {"room_id": room_id, "enabled": body["enabled"], "replayed": False}

    # ----------------------------------------------------------------- who may see a person's task
    def _tr2_task_hidden(self, pr, room: dict, t: dict) -> bool:
        """A task for a person is for the assignee, the room owner and whoever created it. Always on (it only matters once
        such a task exists), so switching the feature off never exposes one."""
        uid = t.get("target_user")
        return bool(uid) and self._tr2_hidden_for(pr, room, uid, t)

    def _tr2_hidden_for(self, pr, room: dict, uid: str, t: dict | None) -> bool:
        if pr.is_agent:
            return not (t and t["by_kind"] == "agent" and t["by_id"] == pr.id)
        if pr.id == uid:
            return False
        m = self._member(room, pr.id)
        if m and m["role"] == "owner":
            return False
        return not (t and t["by_kind"] == "user" and t["by_id"] == pr.id)

    def _tr2_event_hidden(self, pr, room: dict, ev: dict) -> bool:
        p = ev["payload"]
        uid = p.get("target_user_id")
        return bool(uid) and ev["event_type"] in ("task_created", "task_updated") and self._tr2_hidden_for(
            pr, room, uid, self._tasks.get(p.get("task_id")))

    def _tr2_event_fields(self, t: dict) -> dict:
        """Extra fields a ``task_updated`` for a person's task must carry so the feed can hide it from the others."""
        return {"target_user_id": t["target_user"]} if t.get("target_user") else {}

    def _tr2_task_json(self, t: dict) -> dict:
        out: dict[str, Any] = {}
        if t.get("role"):
            out.update(role=t["role"], team_id=t.get("team_id"), routed_to=t.get("routed_to"))
        if t.get("target_user"):
            out.update(target_kind="user", target_user_id=t["target_user"])
            lease = t.get("lease")
            if lease is not None:
                out["lease"] = {"seconds": lease["seconds"], "expires_at": self._iso(lease["expires"]),
                                "policy": lease["policy"], "reminders_sent": len(lease["sent"])}
            out["hard_expiry"] = self._iso(t["hard_expiry"])
        if t.get("escalated_reason"):
            out["escalated_reason"] = t["escalated_reason"]
        return out

    # ----------------------------------------------------------------- my tasks / respond
    def _tr2_person(self, pr, room_id: str, what: str):
        if pr.is_agent:
            raise _err(404, "not found")
        room, _ = self._authorize(pr, room_id, "read", what)
        return room

    def my_tasks(self, pr, room_id: str) -> dict:
        self._tr2_need_on()
        with self._lock:
            room = self._tr2_person(pr, room_id, "task.mine")
            out = []
            for tid in list(self._tasks_by_user.get(pr.id, ())):
                t = self._tasks.get(tid)
                if not t or t["room_id"] != room_id or t.get("target_user") != pr.id:
                    continue
                status = self._task_status(t)
                if status not in ("pending", "accepted", "blocked"):
                    continue
                lease = t["lease"]
                out.append({"task_id": tid, "room_id": room_id, "text": t["text"], "status": status, "role": t.get("role"),
                            "team_id": t.get("team_id"), "created_at": t["created_at"],
                            "lease": {"seconds": lease["seconds"], "expires_at": self._iso(lease["expires"]),
                                      "live": status == "pending"},
                            "hard_expiry": self._iso(t["hard_expiry"]), "reminders_sent": len(lease["sent"]),
                            "no_response_policy": lease["policy"], "untrusted": True})
            out.sort(key=lambda r: r["created_at"], reverse=True)
            _ = room
            return {"tasks": out}

    def respond_task(self, pr, room_id: str, task_id: str, body: dict) -> dict:
        self._tr2_need_on()
        with self._lock:
            room = self._tr2_person(pr, room_id, "task.respond")
            t = self._tasks.get(task_id) if isinstance(task_id, str) else None
            if not t or t["room_id"] != room_id or t.get("target_user") != pr.id or not self._ok(pr.id):
                raise _err(404, "not found")
            if set(body) - {"status", "result"}:
                raise _err(400, "unknown field")
            status = body.get("status")
            result = self._task_text(body.get("result"), "result", required=False)
            cur = self._task_status(t)
            if status == cur and status in ("done", "declined") and result == t.get("result"):
                return {"task_id": task_id, "status": status, "replayed": True}
            allowed = {"pending": ("accepted", "declined"), "accepted": ("done", "declined")}.get(cur, ())
            if not isinstance(status, str) or status not in allowed:
                raise _err(400, "invalid status change")
            self._throttle("task-respond:" + pr.key, 60, 60.0, "task response")
            self._commit("task_updated", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, task_id=task_id, status=status, result=result,
                target_agent_id=None, target_user_id=pr.id, visibility_scope="room", action="task.respond",
                outcome="allowed", project_ids=[room["project_id"]], resource_id=task_id))
            return {"task_id": task_id, "status": status, "replayed": False}

    # ----------------------------------------------------------------- routing
    def _tr2_wants(self, body: dict) -> bool:
        return bool(ROLE_FIELDS & set(body))

    def _tr2_candidates(self, room_id: str, room: dict, team_id: str, role: str, *, exclude: set[str]) -> list[tuple[int, str]]:
        """(open tasks, key) of every holder that may be handed a task now, least loaded first, key order on a tie."""
        out = []
        for key in self._tr2_holder_keys(room_id, team_id, role):
            if key in exclude or not self._tr2_can_take(room, key):
                continue
            load = self._tr2_open_load(key)
            cap = MAX_OPEN_PER_PERSON if key.startswith("user:") else self._max_open_agent()
            if load < cap:
                out.append((load, key))
        out.sort()
        return out

    @staticmethod
    def _max_open_agent() -> int:
        from .collab import MAX_OPEN_TASKS_PER_AGENT
        return MAX_OPEN_TASKS_PER_AGENT

    def _tr2_team_for(self, room_id: str, role: str, team_id: Any) -> str:
        if team_id is not None:
            if not isinstance(team_id, str) or self._tr2_attached(room_id, team_id) is None:
                raise _err(404, "no such team in this room")
            return team_id
        having = [t for t in self._tr_by_room.get(room_id, ()) if self._tr2_holder_keys(room_id, t, role)]
        if not having:
            raise _err(409, f"nobody holds the role {role} in this room", code="no_role_holder")
        if len(having) > 1:
            raise _err(400, f"more than one attached team holds the role {role}: name team_id", code="team_required")
        return having[0]

    # ----------------------------------------------------------------- independent review
    def _tr2_groups(self) -> list[frozenset]:
        return [frozenset(g) for g in self.team_roles_cfg()["reviewer_groups"]]

    def _tr2_disqualified(self, work: dict) -> dict[str, str]:
        """key -> why that principal cannot review ``work``: the one who did it (and anyone who held it before), the one who
        handed it out and whoever handed out the chain above it. A reviewer group counts once."""
        why: dict[str, str] = {}
        if work.get("target_user"):
            why["user:" + work["target_user"]] = "did the work"
        elif work.get("target"):
            why["agent:" + work["target"]] = "did the work"
        for key in work.get("prior", ()):
            why.setdefault(key, "did the work")
        cur, seen = work, set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            why.setdefault(f"{cur['by_kind']}:{cur['by_id']}", "handed out the work")
            cur = self._tasks.get(cur["parent"]) if cur.get("parent") else None
        for group in self._tr2_groups():
            hit = [k for k in group if k in why]
            if hit:
                for k in group:
                    why.setdefault(k, "in the same reviewer group as " + hit[0])
        return why

    def _tr2_work(self, room_id: str, reviews: Any) -> dict:
        work = self._tasks.get(reviews) if isinstance(reviews, str) else None
        if not work or work["room_id"] != room_id:
            raise _err(404, "no such task in this room")
        return work

    def _tr2_check_review(self, pr, room_id: str, reviewer_key: str, reviews: Any) -> None:
        """The flag-on ``_check_independent_review``, over principal keys so people count as well as agents."""
        work = self._tr2_work(room_id, reviews)
        why = self._tr2_disqualified(work).get(reviewer_key)
        if why:
            if why.startswith("in the same"):
                raise _err(400, "a reviewer must not be " + why)
            raise _err(400, f"a reviewer must not be the principal that {why}")
        if pr.is_agent and work.get("target") == pr.id:
            raise _err(400, "an agent cannot choose the reviewer of its own work")

    def _check_review_for_agent(self, pr, room_id: str, reviewer_id: str | None, reviews: Any) -> None:
        if self.team_roles_active():
            self._tr2_check_review(pr, room_id, "agent:" + str(reviewer_id), reviews)
        else:
            self._check_independent_review(pr, room_id, reviewer_id, reviews)

    # ----------------------------------------------------------------- create (role or person targets)
    def _tr2_create(self, pr, room: dict, room_id: str, body: dict) -> dict:
        from .collab import _KEY_RE, MAX_TASK_DEPTH, TASK_RATE, _digest
        cfg = self.team_roles_cfg()
        if set(body) - (_TASK_FIELDS | ROLE_FIELDS):
            raise _err(400, "unknown field")
        role, uid = body.get("target_role"), body.get("target_user_id")
        if (role is None) == (uid is None) or body.get("target_agent_id") is not None:
            raise _err(400, "name the target one way: target_role, target_user_id or target_agent_id")
        if role is None and body.get("team_id") is not None:
            raise _err(400, "team_id goes with target_role")
        if uid is not None and pr.is_agent:
            raise _err(403, "an agent hands work to a role, not to a named person")
        if role is not None and (not isinstance(role, str) or role not in TEAM_ROLE_ACTIONS):
            raise _err(400, "target_role must be one of: " + ", ".join(sorted(TEAM_ROLE_ACTIONS)))
        if uid is not None and not isinstance(uid, str):
            raise _err(400, "target_user_id must be a person id")
        owner = not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "owner"
        lease_s, policy = body.get("lease_seconds"), body.get("no_response_policy")
        if (lease_s is not None or policy is not None) and not owner:
            raise _err(400, "only the room owner sets lease_seconds and no_response_policy")
        if lease_s is not None and (isinstance(lease_s, bool) or not isinstance(lease_s, int) or not MIN_LEASE <= lease_s <= MAX_LEASE):
            raise _err(400, f"lease_seconds must be an integer {MIN_LEASE}-{MAX_LEASE}")
        if policy is not None and (not isinstance(policy, str) or policy not in POLICIES):
            raise _err(400, "no_response_policy must be one of: " + ", ".join(POLICIES))
        reviews = body.get("reviews_task_id")
        if role == "reviewer" and reviews is None:
            raise _err(400, "a reviewer task must name the work it reviews: reviews_task_id")
        text = self._task_text(body.get("text"), "text")
        ikey = body.get("idempotency_key")
        if ikey is not None and (not isinstance(ikey, str) or not _KEY_RE.match(ikey)):
            raise _err(400, "invalid idempotency_key")
        fp = _digest(json.dumps([room_id, "role" if role else "user", body.get("team_id"), role, uid, text, reviews,
                                 lease_s, policy]))
        if ikey is not None and (prior := self._idem.get((pr.key, room_id, "task:" + ikey))):
            if prior[0] != fp:
                raise _err(409, "idempotency_key already used for a different task")
            t0 = self._tasks.get(prior[1]) or {}
            return {"task_id": prior[1], "status": "pending", "replayed": True, "routed_to": t0.get("routed_to")}
        depth, parent, root = 0, body.get("parent_task_id"), pr.owner
        pt = None
        if pr.is_agent:
            pt = self._tasks.get(parent) if isinstance(parent, str) else None
            if (not pt or pt["room_id"] != room_id or pt["target"] != pr.id or self._task_status(pt) != "accepted"):
                raise _err(400, "agents delegate: parent_task_id must be a task you accepted in this room")
            depth, root = pt["depth"] + 1, pt["root"]
            if depth > MAX_TASK_DEPTH:
                raise _err(400, "task chain is too deep")
        elif parent is not None:
            raise _err(400, "parent_task_id is for agents delegating a task")
        self._prune()
        work = self._tr2_work(room_id, reviews) if reviews is not None else None
        why = self._tr2_disqualified(work) if work is not None else {}
        team_id = None
        if role is not None:
            team_id = self._tr2_team_for(room_id, role, body.get("team_id"))
            exclude = set(why)
            if pr.is_agent:
                exclude |= {"agent:" + pr.id, f"{pt['by_kind']}:{pt['by_id']}"}
            cands = self._tr2_candidates(room_id, room, team_id, role, exclude=exclude)
            if not cands:
                raise _err(409, f"no holder of the role {role} can take this task now", code="no_role_holder")
            key = cands[0][1]
        else:
            m = self._member(room, uid)
            if not m or not self._ok(uid):
                raise _err(404, "no such person in this room")
            key = "user:" + uid
            if self._tr2_open_load(key) >= MAX_OPEN_PER_PERSON:
                raise _err(429, "that person has too many open tasks")
            if work is not None:
                self._tr2_check_review(pr, room_id, key, reviews)
        self._throttle("task:" + pr.key, TASK_RATE, 600.0, "task")
        self._throttle("task-owner:" + pr.owner, TASK_RATE * 2, 600.0, "task")
        now = self._now()
        task_id = ids.new_id("grant")
        extra: dict[str, Any] = {"routed_to": key}
        if role is not None:
            extra.update(target_role=role, team_id=team_id)
        if key.startswith("user:"):
            seconds = lease_s or cfg["person_lease_seconds"]
            pol = policy or (self._tr2_team_policy(room_id, team_id) if team_id else cfg["default_no_response_policy"])
            extra.update(target_kind="user", target_user_id=key[5:], lease_seconds=seconds, lease_start_epoch=now,
                         lease_expires_epoch=now + seconds, hard_expiry_epoch=now + max(cfg["hard_expiry_seconds"], seconds),
                         reminder_fractions=list(cfg["reminder_fractions"]), no_response_policy=pol,
                         max_extensions=cfg["max_lease_extensions"])
        else:
            extra["target_kind"] = "agent"
        ev = self._commit("task_created", self._actor(
            pr, project_id=room["project_id"], room_id=room_id, task_id=task_id,
            target_agent_id=key[6:] if key.startswith("agent:") else None, text=text, depth=depth, parent_task_id=parent,
            created_by_kind=pr.kind, created_by_id=pr.id, created_epoch=now, root_user_id=root,
            visibility_scope="room", action="task.create", outcome="allowed", project_ids=[room["project_id"]],
            resource_id=task_id, **({"reviews_task_id": reviews} if reviews is not None else {}),
            **({"idempotency_key": ikey, "fingerprint": fp, "sender_key": pr.key} if ikey is not None else {}), **extra))
        if ikey is not None:
            self._idem[(pr.key, room_id, "task:" + ikey)] = (fp, task_id)
        return {"task_id": task_id, "status": "pending", "routed_to": key, "replayed": False, "event": self._view(ev)}

    # ----------------------------------------------------------------- the timer
    def _tr2_holder_gone(self, t: dict, room: dict | None) -> bool:
        uid = t["target_user"]
        if room is None or not self._member(room, uid) or not self._ok(uid):
            return True
        if t.get("role") and t.get("team_id"):
            return uid not in self._tr2_people.get((t["room_id"], t["team_id"]), {}).get(t["role"], ()) \
                or self._tr2_attached(t["room_id"], t["team_id"]) is None
        return False

    def sweep_team_roles(self) -> dict:
        """Evaluate every open person task against ``now``. Idempotent and crash-safe: the deadline, the reminders sent and
        the policy are all in the replayed state, and each outcome is an event that changes what the next sweep reads."""
        if not self.team_roles_active():
            return {"active": False}
        if self.degraded:
            return {"active": True, "degraded": True}
        from .collab import CollabError
        out = {"active": True, "reminded": 0, "reassigned": 0, "escalated": 0, "extended": 0}
        with self._lock:
            for tid in list(self._tasks):
                t = self._tasks.get(tid)
                if not t or not t.get("target_user") or t.get("lease") is None:
                    continue
                status = self._task_status(t)
                if status not in ("pending", "accepted"):
                    continue
                room = self._rooms.get(t["room_id"])
                if room is None:
                    continue
                try:
                    self._tr2_sweep_one(tid, t, room, status, out)
                except CollabError:
                    break                      # log full or unavailable: stop quietly, the next sweep tries again
        return out

    def _tr2_sweep_one(self, tid: str, t: dict, room: dict, status: str, out: dict) -> None:
        now, lease = self._now(), t["lease"]
        gone = self._tr2_holder_gone(t, room)
        base = dict(room_id=t["room_id"], project_id=room["project_id"], task_id=tid, visibility_scope="room",
                    outcome="allowed", project_ids=[room["project_id"]], resource_id=tid, actor_user_id=None,
                    actor_agent_id=None, actor_kind="system", target_user_id=t["target_user"])
        if status == "accepted":                          # the lease ended on accept; only a holder who vanished matters
            if gone:
                self._tr2_escalate(base, "holder_gone", lease["policy"], out)
            return
        if not gone and now < lease["expires"]:
            span = lease["expires"] - lease["start"]
            for i, frac in enumerate(lease["fractions"]):
                if i not in lease["sent"] and now >= lease["start"] + round(frac * span):
                    self._commit("task_reminded", {**base, "which": i, "due_epoch": now, "action": "task.remind"})
                    out["reminded"] += 1
            return
        reason, policy = ("holder_gone" if gone else "no_response"), lease["policy"]
        if policy == "reassign" and t.get("role") and lease["reassigned"] == 0:
            exclude = set(t.get("prior", ())) | {"user:" + t["target_user"]}
            if t.get("reviews") in self._tasks:
                exclude |= set(self._tr2_disqualified(self._tasks[t["reviews"]]))
            cands = self._tr2_candidates(t["room_id"], room, t["team_id"], t["role"], exclude=exclude) if t.get("team_id") else []
            if cands:
                key = cands[0][1]
                new_end = now + lease["seconds"]
                self._commit("task_reassigned", {**base, "from": "user:" + t["target_user"], "to": key, "reason": reason,
                                                 "at_epoch": now, "new_expires_epoch": new_end, "action": "task.reassign"})
                out["reassigned"] += 1
                return
        elif (policy == "wait" and not gone and lease["ext"] < lease["max_ext"] and lease["expires"] < t["hard_expiry"]):
            new_end = min(lease["expires"] + lease["seconds"], t["hard_expiry"])
            self._commit("task_lease_extended", {**base, "new_expires_epoch": new_end, "extension": lease["ext"] + 1,
                                                 "action": "task.extend"})
            out["extended"] += 1
            return
        self._tr2_escalate(base, reason, policy, out)

    def _tr2_escalate(self, base: dict, reason: str, policy: str, out: dict) -> None:
        self._commit("task_escalated", {**base, "reason": reason, "policy": policy, "action": "task.escalate"})
        out["escalated"] += 1

    # ----------------------------------------------------------------- attention rows
    def _tr2_attention_rows(self, pr, room_id: str, role: str | None) -> list[dict]:
        if not self.team_roles_active():
            return []
        now, out = self._now(), []
        for tid in list(self._tasks_by_user.get(pr.id, ())):
            t = self._tasks.get(tid)
            if not t or t["room_id"] != room_id or t.get("target_user") != pr.id:
                continue
            st = self._task_status(t)
            if st in ("pending", "accepted"):
                lease = t["lease"]
                out.append({"id": tid, "kind": "task_overdue" if st == "pending" and now >= lease["expires"] else "task_assigned",
                            "at": t["created_at"], "expires_epoch": lease["expires"], "role": t.get("role")})
        if role == "owner":
            for tid, t in list(self._tasks.items())[-200:]:
                if t["room_id"] == room_id and t.get("target_user") and t["status"] == "blocked" \
                        and self._task_status(t) == "blocked":
                    out.append({"id": tid, "kind": "task_overdue", "at": t["updated_at"], "expires_epoch": None,
                                "role": t.get("role"), "reason": t.get("escalated_reason")})
        return out
