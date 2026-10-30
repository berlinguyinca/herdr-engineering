"""Rakazo groups and agent bots over Herdr collaboration rooms.

A Rakazo *group* is a collaboration room (1:1: the group id is the room id). A Rakazo *agent bot* is a collaboration agent the
account owns; the other people in a room appear as bot-shaped members because a Rakazo group member is always ``{botId, ...}``.
The bridge acts as the account's configured ``collab_user`` and nothing more: every call goes through the collaboration
service's own checks (room role, agent grant, ceiling), so the phone can do exactly what the dashboard can. A room belongs to
the Rakazo space recorded for it (default: the default space); the app's group and bot lists show only that space's rooms.

Peer text is untrusted data: it is shown as text and never executed.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any

from . import rakazo_events as ev
from . import rakazo_spaces as sp
from .collab import CollabError, Principal

_CODES = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
          429: "TOO_MANY_REQUESTS", 503: "SERVICE_UNAVAILABLE"}
_PALETTE = ("#4f46e5", "#0891b2", "#059669", "#d97706", "#dc2626", "#7c3aed", "#db2777", "#475569")
CHAT_ACTIONS = ["read", "post"]            # what the app grants an agent it adds to a group; nothing that can run commands
TAIL = 100
MAX_PAGE = 50
MAX_ROOMS = 100
SPACE_KIND, ARCHIVED_KIND, PINNED_KIND, NAME_KIND, READ_KIND, REMOVED_KIND = (
    "group_space", "group_archived", "group_pinned", "group_name", "group_read", "group_removed")
PROJECT_KIND = "rooms_project"
PROPOSAL_PREFIX = "proposal:"
STEER_PREFIX = "steer:"


def person_bot_id(user_id: str) -> str:
    return "usr_" + hashlib.sha256(b"rakazo-person:" + user_id.encode()).hexdigest()[:20]


def color_for(key: str) -> str:
    return _PALETTE[int(hashlib.sha256(key.encode()).hexdigest()[:4], 16) % len(_PALETTE)]


def thread_id_for(room_id: str) -> str:
    return "thr_" + room_id


class Rooms:
    """The groups and agent-bot procedures. ``bridge`` supplies the error type, the prefs store and the collab service."""

    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.collab = bridge.collab
        self.prefs = bridge.prefs

    # plumbing -----------------------------------------------------------------------------------------------
    def procs(self) -> dict[str, Any]:
        return {"groups/list": self.groups_list, "groups/listArchived": self.groups_list_archived,
                "groups/get": self.groups_get, "groups/create": self.groups_create, "groups/update": self.groups_update,
                "groups/archive": self.groups_archive, "groups/restore": self.groups_restore,
                "groups/remove": self.groups_remove, "groups/duplicate": self.groups_duplicate,
                "messaging/status": self.messaging_status, "messaging/installations/list": self.installations_list,
                "messaging/installations/remove": self.installations_remove}

    # outside bridges (external agents) as the app's messaging installations ------------------------------------------
    def _externals(self, ctx) -> list[dict[str, Any]]:
        pr = self.principal(ctx, required=False)
        return [] if pr is None else [a for a in self.my_agents(ctx, pr) if a.get("external")]

    @staticmethod
    def _where(info: dict[str, Any]) -> str:
        return (info.get("rooms") or [{"name": ""}])[0]["name"]

    def messaging_status(self, ctx, inp):
        return {"enabled": self.linked(ctx), "providers": [], "openSignup": False, "addresses": {},
                "identities": [{"id": a["agent_id"], "provider": a.get("harness_type") or "external",
                                "address": self._where(a), "botId": a["agent_id"], "botName": a["name"]}
                               for a in self._externals(ctx)]}

    def installations_list(self, ctx, inp):
        return {"available": self.linked(ctx), "personalLinkAvailable": False,
                "installations": [{"id": a["agent_id"], "provider": a.get("harness_type") or "external",
                                   "workspaceName": self._where(a), "botId": a["agent_id"], "botName": a["name"]}
                                  for a in self._externals(ctx)]}

    def installations_remove(self, ctx, inp):
        """Cut an outside bridge off (revokes its credential; what it already posted stays)."""
        pr = self.principal(ctx)
        mine = {a["agent_id"]: a for a in self._externals(ctx)}
        info = mine.get(inp.get("id"))
        if info is None or not info.get("rooms"):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self.call(self.collab.revoke_external, pr, info["rooms"][0]["room_id"], info["agent_id"])
        return {"ok": True}

    def linked(self, ctx) -> bool:
        return bool(ctx.account.collab_user and self.collab is not None)

    def principal(self, ctx, *, required: bool = True) -> Principal | None:
        if not self.linked(ctx):
            if required:
                raise self.Error(400, "BAD_REQUEST", "Collaboration is not linked to this account. "
                                 "The operator sets collab_user for it in the bridge configuration.")
            return None
        pr = Principal("user", ctx.account.collab_user, ctx.account.collab_user)
        if not self.collab.still_valid(pr):
            raise self.Error(403, "FORBIDDEN", "This account's collaboration user is not available.")
        return pr

    def call(self, fn, *args, **kw):
        """Run a collaboration call; its refusals become the bridge's own error shape (404s stay 404)."""
        try:
            return fn(*args, **kw)
        except CollabError as exc:
            status = exc.status if exc.status in _CODES else 400
            raise self.Error(status, _CODES[status], "Not found." if status == 404 else exc.message) from None

    def _pref(self, ctx, kind, key, default=None):
        return self.prefs.get(ctx.account.account_id, "-", kind, key, default)

    def _set(self, ctx, kind, key, value):
        self.b.prefs_put(ctx.account.account_id, "-", kind, key, value)

    # directory of agents and people ---------------------------------------------------------------------------
    def my_agents(self, ctx, pr) -> list[dict[str, Any]]:
        return [a for a in self.call(self.collab.list_my_agents, pr)["agents"] if a["status"] == "active"]

    def agent_bot(self, ctx, info: dict[str, Any], room_name: str | None = None) -> dict[str, Any]:
        """An owned collaboration agent as a Rakazo ``Bot`` (special bot). Its thread is its room's thread."""
        rooms = info.get("rooms") or []
        stamp = datetime.now(UTC).isoformat()
        thread = thread_id_for(rooms[0]["room_id"]) if rooms else "thr_agent_" + info["agent_id"]
        bot = ev.bot_view(_Shim(info), thread, "", "running", ctx.space)
        bot.update({"id": info["agent_id"], "name": info["name"], "title": info.get("harness_type", ""),
                    "description": f"{info.get('harness_type', 'agent')} agent"
                                   + (" (external)" if info.get("external") else ""),
                    "instructions": "", "color": color_for(info["agent_id"]), "computerMode": "dedicated",
                    "spawnKey": info["agent_id"], "status": "running" if info.get("online") else "idle",
                    "updatedAt": stamp, "createdAt": stamp})
        return bot

    def agent_bots(self, ctx) -> list[dict[str, Any]]:
        pr = self.principal(ctx, required=False)
        if pr is None or ctx.space != sp.DEFAULT_SPACE_ID:
            return []
        return [self.agent_bot(ctx, a) for a in self.my_agents(ctx, pr)]

    def find_agent(self, ctx, bot_id: Any) -> dict[str, Any] | None:
        pr = self.principal(ctx, required=False)
        if pr is None or not isinstance(bot_id, str):
            return None
        return next((a for a in self.my_agents(ctx, pr) if a["agent_id"] == bot_id), None)

    # views -------------------------------------------------------------------------------------------------------
    def members(self, ctx, pr, room_id: str) -> list[dict[str, Any]]:
        out = []
        for a in self.call(self.collab.list_room_agents, pr, room_id)["agents"]:
            out.append({"botId": a["agent_id"], "name": a["name"], "color": color_for(a["agent_id"]),
                        "status": "running" if a.get("online") else "idle"})
        for p in self.call(self.collab.room_people, pr, room_id):
            if p["user_id"] != pr.id:
                out.append({"botId": person_bot_id(p["user_id"]), "name": p["handle"], "color": color_for(p["user_id"]),
                            "status": "running" if p.get("online") else "idle"})
        return out

    def message(self, event: dict[str, Any], me: str, thread_id: str) -> dict[str, Any] | None:
        if event.get("event_type") != "message":
            return None
        s = event["sender"]
        mine = s["type"] == "user" and s["id"] == me
        view = {"id": event["event_id"], "threadId": thread_id, "seq": event["sequence"],
                "role": "user" if mine else "bot", "blocks": [{"kind": "text", "text": event["text"]}],
                "createdAt": event["created_at"]}
        if not mine:
            view["botId"] = person_bot_id(s["id"]) if s["type"] == "user" else s["id"]
        return view

    def stream_event(self, view: dict[str, Any], ctx) -> dict[str, Any]:
        return {"id": f"{view['threadId']}:{view['seq']}", "spaceId": ctx.space, "threadId": view["threadId"],
                "botId": view.get("botId", ""), "seq": view["seq"], "type": "thread.message.created",
                "createdAt": view["createdAt"], "payload": {"messageId": view["id"], "seq": view["seq"],
                                                            "role": view["role"], "blocks": view["blocks"],
                                                            "botId": view.get("botId", "")}}

    def group_view(self, ctx, pr, room: dict[str, Any]) -> dict[str, Any]:
        rid = room["room_id"]
        tail = self.call(self.collab.read_tail, pr, rid, TAIL)["events"]
        first = self.call(self.collab.read_events, pr, rid, 0, 1)["events"]
        msgs = [m for m in (self.message(e, pr.id, thread_id_for(rid)) for e in tail) if m]
        last_seq = tail[-1]["sequence"] if tail else 0
        last_stamp = tail[-1]["created_at"] if tail else (first[0]["created_at"] if first else "")
        created = first[0]["created_at"] if first else last_stamp
        read = self._pref(ctx, READ_KIND, rid, 0)
        return {"id": rid, "spaceId": ctx.space, "name": self._pref(ctx, NAME_KIND, rid, room["name"]),
                "pinned": bool(self._pref(ctx, PINNED_KIND, rid, False)), "sectionId": None,
                "archivedAt": self._pref(ctx, ARCHIVED_KIND, rid, None),
                "members": self.members(ctx, pr, rid), "threadId": thread_id_for(rid),
                "preview": ev.snippet(msgs[-1]["blocks"][0]["text"], 120) if msgs else "",
                "unread": bool(msgs) and msgs[-1]["role"] != "user" and last_seq > read,
                "updatedAt": last_stamp, "createdAt": created}

    def my_rooms(self, ctx, pr, *, archived: bool) -> list[dict[str, Any]]:
        out = []
        for room in self.call(self.collab.list_rooms, pr)["rooms"][:MAX_ROOMS]:
            rid = room["room_id"]
            if self._pref(ctx, SPACE_KIND, rid, sp.DEFAULT_SPACE_ID) != ctx.space or self._pref(ctx, REMOVED_KIND, rid, False):
                continue
            if bool(self._pref(ctx, ARCHIVED_KIND, rid, None)) == archived:
                out.append(room)
        return out

    def room(self, ctx, pr, group_id: Any) -> dict[str, Any]:
        """A room of this account that belongs to the active space, else 404 (the same for 'not yours' and 'elsewhere')."""
        if not isinstance(group_id, str):
            raise self.Error(400, "BAD_REQUEST", "Provide a groupId.")
        for room in self.call(self.collab.list_rooms, pr)["rooms"]:
            if room["room_id"] == group_id:
                if (self._pref(ctx, SPACE_KIND, group_id, sp.DEFAULT_SPACE_ID) != ctx.space
                        or self._pref(ctx, REMOVED_KIND, group_id, False)):
                    break
                return room
        raise self.Error(404, "NOT_FOUND", "Not found.")

    # group procedures ---------------------------------------------------------------------------------------
    def auto_join(self, ctx, pr) -> None:
        """Operator opt-in (``rakazo_bridge.auto_join_rooms``): the person's own active agents are added to those rooms with
        chat rights only, so a new agent shows up there without a tap. A room the person is not in is skipped; failures
        never break the listing."""
        wanted = set(getattr(self.b, "auto_join_rooms", ()))
        if not wanted:
            return
        try:
            mine = [a["agent_id"] for a in self.my_agents(ctx, pr)]
            for room in self.call(self.collab.list_rooms, pr)["rooms"][:MAX_ROOMS]:
                if room["room_id"] not in wanted:
                    continue
                have = {a["agent_id"] for a in self.call(self.collab.list_room_agents, pr, room["room_id"])["agents"]}
                for bot_id in mine:
                    if bot_id not in have:
                        self.call(self.collab.grant_agent, pr, room["room_id"], {"agent_id": bot_id, "actions": CHAT_ACTIONS})
        except Exception:  # noqa: BLE001 - best effort; the explicit group procedures remain the source of truth
            return

    def groups_list(self, ctx, inp):
        pr = self.principal(ctx, required=False)
        if pr is not None:
            self.auto_join(ctx, pr)
        return [] if pr is None else [self.group_view(ctx, pr, r) for r in self.my_rooms(ctx, pr, archived=False)]

    def groups_list_archived(self, ctx, inp):
        pr = self.principal(ctx, required=False)
        return [] if pr is None else [self.group_view(ctx, pr, r) for r in self.my_rooms(ctx, pr, archived=True)]

    def groups_get(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        view = self.group_view(ctx, pr, room)
        msgs = [m for m in (self.message(e, pr.id, view["threadId"])
                            for e in self.call(self.collab.read_tail, pr, room["room_id"], TAIL)["events"]) if m]
        return {**view, "messages": msgs[-MAX_PAGE:]}

    def _new_room(self, ctx, pr, name: str) -> dict[str, Any]:
        """Create a room in the project the app's groups live in (one per account, made on first use)."""
        for attempt in (0, 1):
            pid = self._pref(ctx, PROJECT_KIND, "project")
            if not pid:
                pid = self.call(self.collab.create_project, pr, {"name": "Rakazo groups"})["project_id"]
                self._set(ctx, PROJECT_KIND, "project", pid)
            try:
                return self.call(self.collab.create_room, pr, pid, {"name": name})
            except self.Error as exc:
                if exc.status != 404 or attempt:        # the stored project is gone or no longer ours: make a new one
                    raise
                self.prefs.delete(ctx.account.account_id, "-", PROJECT_KIND, "project")
        raise self.Error(500, "INTERNAL_SERVER_ERROR", "The group could not be created.")

    def _add_agents(self, ctx, pr, room_id: str, bot_ids: list[Any]) -> None:
        mine = {a["agent_id"] for a in self.my_agents(ctx, pr)}
        for bot_id in bot_ids:
            if bot_id not in mine:
                raise self.Error(400, "BAD_REQUEST", "Only your own agents can be added to a group.")
        for bot_id in bot_ids:
            self.call(self.collab.grant_agent, pr, room_id, {"agent_id": bot_id, "actions": CHAT_ACTIONS})

    def groups_create(self, ctx, inp):
        pr = self.principal(ctx)
        name, bot_ids = inp.get("name"), inp.get("botIds")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise self.Error(400, "BAD_REQUEST", "A group needs a name of up to 80 characters.")
        if not isinstance(bot_ids, list) or not 2 <= len(bot_ids) <= 10 or len(set(bot_ids)) != len(bot_ids):
            raise self.Error(400, "BAD_REQUEST", "A group needs 2 to 10 different members.")
        mine = {a["agent_id"] for a in self.my_agents(ctx, pr)}
        if not all(b in mine for b in bot_ids):
            raise self.Error(400, "BAD_REQUEST", "Only your own agents can be added to a group.")
        room = self._new_room(ctx, pr, name.strip())
        self._set(ctx, SPACE_KIND, room["room_id"], ctx.space)
        self._add_agents(ctx, pr, room["room_id"], bot_ids)
        return self.group_view(ctx, pr, {"room_id": room["room_id"], "name": room["name"]})

    def groups_update(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        rid = room["room_id"]
        if "botIds" in inp:
            wanted = inp["botIds"]
            if not isinstance(wanted, list) or not 2 <= len(wanted) <= 10 or len(set(wanted)) != len(wanted):
                raise self.Error(400, "BAD_REQUEST", "A group needs 2 to 10 different members.")
            current = [a["agent_id"] for a in self.call(self.collab.list_room_agents, pr, rid)["agents"]]
            expected = inp.get("expectedBotIds")
            if isinstance(expected, list) and set(expected) != set(current):
                raise self.Error(409, "CONFLICT", "The group changed. Reload it and try again.")
            self._add_agents(ctx, pr, rid, [b for b in wanted if b not in current])
            for gone in current:
                if gone not in wanted:
                    self.call(self.collab.remove_agent_from_room, pr, rid, gone)
        if isinstance(inp.get("name"), str) and inp["name"].strip():
            self._set(ctx, NAME_KIND, rid, inp["name"].strip()[:80])
        if isinstance(inp.get("pinned"), bool):
            self._set(ctx, PINNED_KIND, rid, inp["pinned"])
        return self.group_view(ctx, pr, room)

    def groups_archive(self, ctx, inp):
        pr = self.principal(ctx)
        self._set(ctx, ARCHIVED_KIND, self.room(ctx, pr, inp.get("groupId"))["room_id"], datetime.now(UTC).isoformat())
        return {"ok": True}

    def groups_restore(self, ctx, inp):
        pr = self.principal(ctx)
        self.prefs.delete(ctx.account.account_id, "-", ARCHIVED_KIND, self.room(ctx, pr, inp.get("groupId"))["room_id"])
        return {"ok": True}

    def groups_remove(self, ctx, inp):
        """Hide the group from the app for good. The room, its history and its members stay in collaboration: a room is
        shared with other people, so this account cannot delete it, only stop seeing it."""
        pr = self.principal(ctx)
        self._set(ctx, REMOVED_KIND, self.room(ctx, pr, inp.get("groupId"))["room_id"], True)
        return {"ok": True}

    def groups_duplicate(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        agents = [a["agent_id"] for a in self.call(self.collab.list_room_agents, pr, room["room_id"])["agents"]
                  if not a.get("external")]
        name = (self._pref(ctx, NAME_KIND, room["room_id"], room["name"]) + " copy")[:80]
        return self.groups_create(ctx, {"name": name, "botIds": agents})

    # thread procedures for a group ----------------------------------------------------------------------------
    def head(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        tail = self.call(self.collab.read_tail, pr, room["room_id"], 1)
        return {"threadId": thread_id_for(room["room_id"]), "cursor": tail["next_cursor"]}

    def thread(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        rid, tid = room["room_id"], thread_id_for(room["room_id"])
        events = self.call(self.collab.read_tail, pr, rid, TAIL)
        msgs = [m for m in (self.message(e, pr.id, tid) for e in events["events"]) if m]
        page = msgs[-MAX_PAGE:]
        older = page[0]["seq"] if page and len(msgs) > len(page) else None
        page += self.proposal_cards(pr, rid, tid, max(events["next_cursor"], 0))
        page += self.steering_cards(pr, rid, tid, max(events["next_cursor"], 0))
        return {"threadId": tid, "cursor": events["next_cursor"], "messages": page, "olderCursor": older,
                "groupId": rid, "groupName": self._pref(ctx, NAME_KIND, rid, room["name"]),
                "members": self.members(ctx, pr, rid), "run": None, "activeRuns": []}

    # delegate proposals as approval cards ---------------------------------------------------------------------
    def proposal_cards(self, pr, room_id: str, tid: str, seq: int) -> list[dict[str, Any]]:
        """Quarantined proposals from delegate agents, as ask cards the room owner can allow or deny. The proposal text is
        untrusted data: shown as a short snippet, never executed. A person who cannot review proposals sees no cards."""
        try:
            listed = self.collab.list_proposals(pr, room_id)["proposals"]
        except CollabError:
            return []
        cards = []
        for p in listed:
            if p.get("state") != "quarantined":
                continue
            block = {"kind": "ask", "text": f"Accept the proposal \"{ev.snippet(str(p.get('title') or ''), 80)}\"?",
                     "approvalEffectId": p["proposal_id"], "actions": [{"id": "allow", "label": "Accept"},
                                                                       {"id": "deny", "label": "Reject"}],
                     "status": "pending", "detail": "Untrusted text from another agent. Accepting records it; it is not run."}
            cards.append({"id": PROPOSAL_PREFIX + p["proposal_id"], "threadId": tid, "seq": seq, "role": "bot",
                          "blocks": [block], "botId": p.get("agent_id") or "", "runId": p.get("task_id"),
                          "createdAt": p.get("created_at") or ""})
        return cards

    # steering requests as approval cards ----------------------------------------------------------------------
    def steering_cards(self, pr, room_id: str, tid: str, seq: int) -> list[dict[str, Any]]:
        """Pending steering requests (an instruction aimed at a running node) as ask cards a room owner can confirm or
        decline. The instruction is untrusted text, shown as a snippet. Confirming only releases it to the node's owner
        process through the existing fenced, single-use confirmation; nothing runs here."""
        try:
            listed = self.collab.list_steering(pr, room_id, {})["steering"]
        except CollabError:
            return []
        cards = []
        for r in listed:
            if r.get("status") != "pending":
                continue
            what = ev.snippet(str(r.get("instruction") or r.get("action") or ""), 80)
            block = {"kind": "ask", "text": f"Confirm steering \"{r['action']}\": {what}?",
                     "approvalEffectId": r["steering_id"], "actions": [{"id": "allow", "label": "Confirm"},
                                                                       {"id": "deny", "label": "Decline"}],
                     "status": "pending", "detail": "Untrusted instruction. Confirming releases it once to the target's owner."}
            cards.append({"id": STEER_PREFIX + r["steering_id"], "threadId": tid, "seq": seq, "role": "bot", "blocks": [block],
                          "createdAt": r.get("proposed_at") or ""})
        return cards

    def _answer_steering(self, pr, room_id: str, steer_id: str, answer: Any):
        if answer not in ("allow", "deny"):
            raise self.Error(400, "BAD_REQUEST", "Answer allow or deny.")
        if answer == "deny":
            self.call(self.collab.reject_steering, pr, room_id, steer_id, {"reason": "declined from the app"})
            return {"ok": True}
        shown = next((r for r in self.call(self.collab.list_steering, pr, room_id, {})["steering"]
                      if r["steering_id"] == steer_id), None)
        if shown is None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self.call(self.collab.confirm_steering, pr, room_id, steer_id,
                  {"digest": shown["digest"], "expected_fence": shown["target"]["fence"]})
        return {"ok": True}

    def answer(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        message_id, answer = inp.get("messageId"), inp.get("answer")
        if isinstance(message_id, str) and message_id.startswith(STEER_PREFIX) and not (inp.get("dismiss") or inp.get("fields")):
            return self._answer_steering(pr, room["room_id"], message_id[len(STEER_PREFIX):], answer)
        if not (isinstance(message_id, str) and message_id.startswith(PROPOSAL_PREFIX)) or inp.get("dismiss") or inp.get("fields"):
            raise self.Error(400, "BAD_REQUEST", "Only proposal and steering answers are supported in groups.")
        decision = {"allow": "accept", "deny": "reject"}.get(answer)
        if decision is None:
            raise self.Error(400, "BAD_REQUEST", "Answer allow or deny.")
        proposal_id = message_id[len(PROPOSAL_PREFIX):]
        shown = self.call(self.collab.get_proposal, pr, room["room_id"], proposal_id)
        self.call(self.collab.decide_proposal, pr, room["room_id"], proposal_id,
                  {"decision": decision, "expected_digest": shown["digest"], "idempotency_key": "rk-" + secrets.token_hex(8)})
        return {"ok": True}

    def messages(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        rid, tid, before = room["room_id"], thread_id_for(room["room_id"]), inp.get("before")
        if before is not None and (isinstance(before, bool) or not isinstance(before, int)):
            raise self.Error(400, "BAD_REQUEST", "before must be an integer.")
        cursor, collected = 0, []
        for _ in range(25):                                   # at most 5000 events: a bound, not a promise of full history
            chunk = self.call(self.collab.read_events, pr, rid, cursor, 200)
            collected += [m for m in (self.message(e, pr.id, tid) for e in chunk["events"]) if m]
            cursor = chunk["next_cursor"]
            if not chunk["has_more"]:
                break
        older_than = [m for m in collected if before is None or m["seq"] < before]
        page = older_than[-MAX_PAGE:]
        more = page[0]["seq"] if page and len(older_than) > len(page) else None
        return {"threadId": tid, "messages": page, "olderCursor": more, "coveredThroughSeq": max(0, cursor)}

    def send(self, ctx, inp, *, agent: dict[str, Any] | None = None):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        rid = room["room_id"]
        text = inp.get("text")
        if inp.get("artifactIds"):
            raise self.Error(400, "BAD_REQUEST", "Attachments are not supported in groups yet.")
        if not isinstance(text, str) or not text.strip():
            raise self.Error(400, "BAD_REQUEST", "Provide text.")
        wanted = [agent["agent_id"]] if agent else []
        for m in inp.get("mentions") or []:
            ref = m if isinstance(m, str) else (m.get("id") if isinstance(m, dict) and m.get("kind") == "bot" else None)
            if isinstance(ref, str):
                wanted.append(ref)
        if wanted:
            roster = {a["agent_id"]: a for a in self.call(self.collab.list_room_agents, pr, rid)["agents"]}
            prefix = " ".join("@" + (roster[w].get("address") or roster[w]["name"]) for w in dict.fromkeys(wanted)
                              if w in roster)
            if prefix:
                text = f"{prefix} {text}"
        key = inp.get("clientNonce") if isinstance(inp.get("clientNonce"), str) and inp["clientNonce"] else None
        status, out = self.call(self.collab.post_message, pr, rid,
                                {"text": text.strip(), "idempotency_key": key or "rk-" + secrets.token_hex(12)})
        event = out["event"]
        return {"taskId": event["event_id"], "runId": event["event_id"], "seq": event["sequence"]}

    def mark_read(self, ctx, inp):
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        self._set(ctx, READ_KIND, room["room_id"], self.call(self.collab.read_tail, pr, room["room_id"], 1)["next_cursor"])
        return {"ok": True, "unreadAfterSeq": None}

    def subscribe(self, ctx, inp, cursor: int):
        """The fetch function for ``threads/subscribe`` on a group: events after ``cursor``, as stream events."""
        pr = self.principal(ctx)
        room = self.room(ctx, pr, inp.get("groupId"))
        rid, tid = room["room_id"], thread_id_for(room["room_id"])

        def fetch(after: int) -> list[dict[str, Any]]:
            chunk = self.call(self.collab.read_events, pr, rid, after, 200)
            return [self.stream_event(m, ctx) for m in (self.message(e, pr.id, tid) for e in chunk["events"]) if m]
        return fetch, lambda: not self.collab.still_valid(pr)


class _Shim:
    """The two attributes ``bot_view`` reads from an assistant, taken from an agent record."""

    def __init__(self, info: dict[str, Any]):
        self.assistant_id, self.name = info["agent_id"], info["name"]
        self.instructions, self.updated_at, self.created_at = "", None, None
