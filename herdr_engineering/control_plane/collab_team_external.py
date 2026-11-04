"""Team collaboration, slice 5 (collab side): the issue target, and who may file, approve and retry an external action.

Design: ``docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md`` (sections 6.1, 10, 11, 14 slice 5, 16 decision 1).
Operation and rollback: ``docs/operations/team-rooms.md``. The approval ledger and the executor are ``external_actions.py`` (they live
in the assistant, contradiction 10); this module is the authorisation in front of them and the audit trail beside them.

* ``team_issue_target_set``: the repository a team's issues are created in. Data on the team (decision 1), owner user only.
* ``external_action_filed`` / ``external_action_decided``: audit records only (they fold into nothing). They carry who acted, the
  action id and the hash key, never the content, a recipient, a message id or a credential.
* Filing: the room owner, a holder of the ``issue_writer`` role (an agent attached with that role, or a person assigned to it), the
  team's coordinator (an operator-owned agent, or an opted-in external coordinator) may PROPOSE. Anyone else on the room is told 403
  with an audit record; a stranger is told 404. The content is built from the operator-approved brief, never from the filer.
* Deciding and retrying: the room OWNER only. A coordinator, an external coordinator, a guest, a contributor, a viewer and every agent
  is told 403 and the refusal is audited under their own identity (design section 14 slice 5 and section 10.2).
* The collab lock is never held across a call into the assistant: filing reads state under it, then calls out, then audits.

Everything sits behind ``control_plane.personal_assistant.external_actions.enabled`` (YAML only, absent means off) and also needs the
slice 1 flag, a wired assistant, and the slice 4 brief it is built from.
"""
from __future__ import annotations

from typing import Any

from .assistant_briefs import REFERENCE
from .external_actions import DRAFT, ISSUE, KINDS, REPO

EA_EVENTS = frozenset({"team_issue_target_set", "external_action_filed", "external_action_decided"})
_FILE_FIELDS = frozenset({"kind", "reference", "title", "labels", "body"})
_ISSUE_ONLY = ("title", "labels")
_DRAFT_ONLY = ("body",)
_FILER_VIEW = ("action_id", "kind", "state", "replayed", "team_id", "created_at")


def _err(status: int, message: str, **kw):
    from .collab import CollabError
    return CollabError(status, message, **kw)


class ExternalActionsMixin:
    """State and operations for slice 5. Mixed into ``CollabService`` after the slice 1 to 4 mixins; it relies on the lock, the log,
    ``_commit``, ``_actor``, ``_audit_denied``, ``_throttle``, ``_member``, ``_effective``, ``_ok`` and the slice 1 to 4 indexes."""

    def _init_external_actions(self) -> None:
        self.external_actions_source = None      # callable -> the assistant's external_actions config; wired by the control plane
        self.external_actions_hook = None        # the in-process bridge to the assistant's ledger; absent = off
        self._ea_target: dict[tuple, dict] = {}  # (room, team) -> {repo, seq}

    # ----------------------------------------------------------------- config (fail closed)
    def external_actions_cfg(self) -> dict | None:
        source = self.external_actions_source
        if source is None or self.external_actions_hook is None:
            return None
        try:
            cfg = source()
        except Exception:                                      # noqa: BLE001 - any failure is OFF
            return None
        return cfg if isinstance(cfg, dict) and cfg.get("enabled") is True and not cfg.get("kill_switch") else None

    def external_actions_active(self) -> bool:
        return bool(self.external_actions_cfg() is not None and self.team_rooms_active())

    def _ea_need_on(self) -> None:
        if not self.external_actions_active():
            raise _err(404, "not found")                       # off means the route does not exist

    # ----------------------------------------------------------------- the fold
    def _apply_external_actions(self, ev: dict) -> bool:
        t, p, seq = ev["event_type"], ev["payload"], ev["sequence"]
        if t not in EA_EVENTS:
            return False
        if t == "team_issue_target_set":
            self._ea_target[(p["room_id"], p["team_id"])] = {"repo": p["repo"], "seq": seq}
        return True                                            # the other two are audit records

    # ----------------------------------------------------------------- the target
    def set_issue_target(self, pr, room_id: str, team_id: str, body: dict) -> dict:
        self._ea_need_on()
        with self._lock:
            room = self._tr_owner(pr, room_id, "team.issue_target.set")
            if self._tr2_attached(room_id, team_id) is None:
                raise _err(404, "not found")
            if not isinstance(body, dict) or set(body) - {"repo"}:
                raise _err(400, "unknown field")
            repo = body.get("repo")
            if not isinstance(repo, str) or not REPO.match(repo):
                raise _err(400, "repo must be 'owner/name'")
            have = self._ea_target.get((room_id, team_id))
            if have is not None and have["repo"] == repo:
                return {"room_id": room_id, "team_id": team_id, "repo": repo, "replayed": True}
            self._throttle("team-issue-target:" + pr.key, 30, 600.0, "issue target")
            self._commit("team_issue_target_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, repo=repo, visibility_scope="room",
                action="team.issue_target.set", outcome="allowed", project_ids=[room["project_id"]], resource_id=team_id))
            return {"room_id": room_id, "team_id": team_id, "repo": repo, "replayed": False}

    def view_issue_target(self, pr, room_id: str, team_id: str) -> dict:
        self._ea_need_on()
        with self._lock:
            self._tr_owner(pr, room_id, "team.issue_target.view")
            if self._tr2_attached(room_id, team_id) is None:
                raise _err(404, "not found")
            have = self._ea_target.get((room_id, team_id))
            return {"room_id": room_id, "team_id": team_id, "repo": have["repo"] if have else None}

    # ----------------------------------------------------------------- who is who
    @staticmethod
    def _ea_owner_id(room: dict) -> str | None:
        return next((uid for uid, m in room["members"].items() if m["role"] == "owner"), None)

    def _ea_filer_in(self, pr, room: dict, room_id: str, team_id: str) -> bool:
        """May this principal PROPOSE for this team: its issue writer, its coordinator, or the room owner."""
        att = self._tr2_attached(room_id, team_id)
        if att is None:
            return False
        if pr.is_agent:
            if att["agents"].get(pr.id) == "issue_writer":
                return True
        else:
            m = self._member(room, pr.id)
            if m is not None and m["role"] == "owner":
                return True
            if pr.key in self._tr2_holder_keys(room_id, team_id, "issue_writer"):
                return True
        coord = self._tc_coord.get((room_id, team_id))
        return bool(coord is not None and coord["holder"] == pr.key and self._tc_holder_active(room, team_id, pr.key))

    def _ea_teams(self, room_id: str) -> list[str]:
        return [t for (r, t), att in self._tr_attach.items() if r == room_id and att["state"] == "attached"]

    def _ea_known(self, pr, room_id: str) -> dict:
        """The room, for a principal that can see it at all; anyone else gets the one 404 of a room that does not exist."""
        room = self._rooms.get(room_id) if isinstance(room_id, str) else None
        if room is None or self._effective(pr, room) is None or (not pr.is_agent and not self._ok(pr.id)):
            raise _err(404, "not found")
        return room

    def _ea_deny(self, pr, room: dict, room_id: str, action: str, reason: str, resource_id: str | None = None):
        self._audit_denied(pr, action, reason, room_id=room_id, project_ids=[room["project_id"]], resource_id=resource_id or room_id)
        self.stats["policy_denied"] += 1
        return _err(403, "forbidden")

    def _ea_owner(self, pr, room_id: str, action: str, resource_id: str | None = None) -> dict:
        """The room owner as a person. Every other principal that can see the room is told 403 and audited."""
        room = self._ea_known(pr, room_id)
        m = None if pr.is_agent else self._member(room, pr.id)
        if m is None or m["role"] != "owner":
            raise self._ea_deny(pr, room, room_id, action, "not_owner", resource_id)
        return room

    # ----------------------------------------------------------------- filing
    def _ea_brief(self, room_id: str, reference: str) -> dict | None:
        for sid in self._sg_by_room.get(room_id, ()):
            sg = self._sg[sid]
            if sg.get("source") == "assistant_brief" and sg.get("reference") == reference:
                return sg
        return None

    def _ea_shape(self, kind: str, body: dict) -> None:
        """What the filer chose, checked before anything is looked up (a malformed field is 400 whoever asks, whatever the state)."""
        from .external_actions import MAX_LABELS, clean_title
        try:
            if kind == ISSUE:
                if body.get("title") is not None:
                    clean_title(body["title"])
                labels = body.get("labels")
                allowed = set(self.external_actions_cfg()["github"]["labels"])
                if labels is not None and (not isinstance(labels, list) or len(labels) > MAX_LABELS
                                           or not all(isinstance(x, str) and x in allowed for x in labels)):
                    raise ValueError("labels must come from the operator's allowed list")
            else:
                text = body.get("body")
                if not isinstance(text, str) or not text.strip() or len(text) > 800:
                    raise ValueError("the reply body must be 1-800 characters of plain text")
        except ValueError as exc:
            raise _err(400, str(exc)) from None

    def file_external_action(self, pr, room_id: str, body: Any, via: str | None = None) -> tuple[int, dict]:
        """Propose one external action for an approved email brief. Returns (status, view): 201 new, 200 a replay."""
        self._ea_need_on()
        if not isinstance(body, dict) or set(body) - _FILE_FIELDS:
            raise _err(400, "unknown field")
        kind, ref = body.get("kind"), body.get("reference")
        if kind not in KINDS:
            raise _err(400, "kind must be one of: " + ", ".join(KINDS))
        if not isinstance(ref, str) or not REFERENCE.match(ref):
            raise _err(400, "reference must be the brief's reference")
        if any(k in body for k in (_DRAFT_ONLY if kind == ISSUE else _ISSUE_ONLY)):
            raise _err(400, "unknown field for this kind of action")
        self._ea_shape(kind, body)
        with self._lock:
            room = self._ea_known(pr, room_id)
            sg = self._ea_brief(room_id, ref)
            if sg is None:
                raise _err(404, "not found")
            team_id = sg["team_id"]
            if not self._ea_filer_in(pr, room, room_id, team_id):
                raise self._ea_deny(pr, room, room_id, "external.file", "not_issue_writer", sg["id"])
            if self._sg_state(sg) not in ("approved", "materialized"):
                raise _err(409, "the brief has not been approved by the room owner", code="brief_not_approved")
            owner = self._ea_owner_id(room)
            ctx = {"brief_key": sg["brief_key"], "room_id": room_id, "team_id": team_id, "suggestion_id": sg["id"]}
            if kind == ISSUE:
                target = self._ea_target.get((room_id, team_id))
                if target is None:
                    raise _err(409, "the owner has not set an issue repository for this team", code="no_issue_target")
                ctx.update(repo=target["repo"], default_title=f"Email brief: {sg['topic']}", title=body.get("title"),
                           labels=body.get("labels"), brief_text=sg["quoted_text"] or sg["text"])
            else:
                ctx.update(reference=ref, body=body.get("body"))
            self._throttle("ext-file:" + pr.key, 30, 600.0, "external action")
            filer = "assistant" if via == "assistant" else pr.key
        out = self.external_actions_hook.file(owner, filer, kind, ctx)
        with self._lock:
            self._commit("external_action_filed", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, team_id=team_id, action_id=out["action_id"], kind=kind,
                action_key=sg["brief_key"][:16], replayed=bool(out["replayed"]), **({"via": via} if via else {}),
                visibility_scope="room", action="external.file", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=out["action_id"]))
        return (200 if out["replayed"] else 201), {k: out[k] for k in _FILER_VIEW}

    # ----------------------------------------------------------------- reading
    def list_external_actions(self, pr, room_id: str) -> dict:
        """The owner sees every card of the room; a filer only the state of what they proposed; anyone else is refused."""
        self._ea_need_on()
        with self._lock:
            room = self._ea_known(pr, room_id)
            owner = self._ea_owner_id(room)
            m = None if pr.is_agent else self._member(room, pr.id)
            is_owner = m is not None and m["role"] == "owner"
            if not is_owner and not any(self._ea_filer_in(pr, room, room_id, t) for t in self._ea_teams(room_id)):
                raise self._ea_deny(pr, room, room_id, "external.list", "not_filer")
        filer = None if is_owner else pr.key
        return {"actions": self.external_actions_hook.rows(owner, room_id, filer)}

    # ----------------------------------------------------------------- deciding and retrying
    def decide_external_action(self, pr, room_id: str, action_id: str, body: Any, op: str = "decision") -> dict:
        """Approve or reject (``op='decision'``) or retry (``op='retry'``) one action. The room OWNER only."""
        self._ea_need_on()
        allowed = {"decision", "expected_sha256"} if op == "decision" else {"expected_sha256"}
        with self._lock:
            room = self._ea_owner(pr, room_id, "external.decide" if op == "decision" else "external.retry",
                                  action_id if isinstance(action_id, str) else None)
            if not isinstance(body, dict) or set(body) - allowed:
                raise _err(400, "unknown field")
            self._throttle("ext-decide:" + pr.key, 60, 600.0, "external action decision")
            owner = pr.id
        out = self.external_actions_hook.act(owner, room_id, action_id, op, body)
        with self._lock:
            self._commit("external_action_decided", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, action_id=action_id, op=op,
                decision=body.get("decision") if op == "decision" else "retry", state=out["state"], replayed=bool(out["replayed"]),
                visibility_scope="room", action="external.decide", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=action_id))
        return out

    def external_action_room(self, owner_user: str, action_id: str) -> str | None:
        """The room an action was filed in (for the assistant door, which has only the action id)."""
        return self.external_actions_hook.room_of(owner_user, action_id) if self.external_actions_hook is not None else None
