"""Room commands (Task C6) and handoff status (Task C7).

The collab server interprets a message as a command ONLY when `/` is its first character and a known name
follows (`/handoff`, `/decide`, `/summarize`, `/issue`, `/assign`); `/` anywhere else, `/usr/bin`, `/handoffx` or
`@agent /skill` is plain text and reaches harnesses unchanged. A command is posted like a message (same
authorization, idempotency key and per-sender/per-owner rate limits) but is committed as its own feed event:

- `handoff {handoff_id, from, to, note}`: any member who may post; `to` is notified (`mentions_me`). It starts
  `pending`; the recipient (or the recipient agent's owner) accepts or declines (`handoff_updated`). Unknown,
  ambiguous or group targets are refused with the composer's wording.
- `decision {text}`: any member who may post; listed by `GET .../decisions` and in room state.
- `summary_request {text}`: a request that an online agent fulfils (the server never calls a model).
- `issue_draft` / `assignment`: approvers only (they write to GitHub). Nothing reaches GitHub until an approver
  confirms the draft (`POST .../drafts/{id}/confirm`), and only through `GitHubWriter`, which refuses unless
  YAML `github.write.enabled` is true. A confirm is idempotent: the draft id is the key, the created issue number
  is recorded in a `github_written` event, and a second click while the first is in flight is refused.

A remote (external) agent's text is never a command: it sees chat messages only, so it could not follow one.
Authorization is derived from the credential everywhere; a request field never names the actor.
"""
from __future__ import annotations

import bisect
import re
from collections import defaultdict
from typing import Any

from . import collab_mentions as mentions
from . import collab_refs as issue_refs
from . import ids
from .redact import redact_text

COMMANDS = ("handoff", "decide", "summarize", "issue", "assign")
_COMMAND = re.compile(r"/(handoff|decide|summarize|issue|assign)(?=\s|\Z)")
_LEADING_NAME = re.compile(r"@([A-Za-z0-9._-]{1,48})(?=\s|\Z)")
_REPO_ARG = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})(?=\s|\Z)")
_HANDLE = re.compile(r"[a-z0-9-]{1,27}\Z")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
COMMAND_EVENTS = frozenset({"handoff", "handoff_updated", "decision", "summary_request", "issue_draft",
                            "assignment", "github_written"})
# events a post creates (the poster's idempotency key replays them)
POSTED_EVENTS = frozenset({"handoff", "decision", "summary_request", "issue_draft", "assignment"})
MAX_SELECTION = 20
ISSUE_TITLE_MAX = 256
ISSUE_BODY_MAX = 20000
MAX_NOTE = 2000
DECISIONS_SHOWN = 50
GITHUB_WRITES = 10              # confirmed writes per principal per GITHUB_WRITE_WINDOW
GITHUB_WRITE_WINDOW = 600.0
HANDOFF_TTL = 7 * 24 * 3600.0   # a pending handoff older than this is shown as expired (display only)
HANDOFF_ANSWERS = 30            # accept/decline per principal per minute
UNREAD_CAP = 99                 # an unread mention count stops counting here
UNREAD_SCAN = 5000              # and looks at most this many of a room's newest events
WAKE_FANOUT = 5                 # agents one group mention (@all, @agents, @here) may wake
WAKE_SCAN = 5000                # a mention wait never looks further back than this many log events


def parse_command(text: Any) -> tuple[str, str] | None:
    """`(name, rest)` when the text is a room command, else None (plain text)."""
    if not isinstance(text, str):
        return None
    m = _COMMAND.match(text)
    return (m.group(1), text[m.end():].strip()) if m else None


def normalize_people(value: Any) -> dict:
    """YAML `control_plane.collab.people`: `{<handle>: {github: <login>}}` (GitHub logins are public)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("control_plane.collab.people must map a person's handle to {github: login}")
    out = {}
    for name, row in value.items():
        handle = str(name).strip().lower()
        if not _HANDLE.match(handle):
            raise ValueError(f"control_plane.collab.people: {name!r} is not a room handle ([a-z0-9-], 1-27)")
        if not isinstance(row, dict) or set(row) - {"github"}:
            raise ValueError(f"control_plane.collab.people.{handle} must be {{github: login}}")
        login = row.get("github")
        if not isinstance(login, str) or not _LOGIN.match(login):
            raise ValueError(f"control_plane.collab.people.{handle}.github must be a GitHub login")
        out[handle] = {"github": login}
    return out


class CommandsMixin:
    def _init_commands(self) -> None:
        self.people: dict = {}
        self.people_source = None            # optional callable -> raw YAML mapping, read per request
        self.github_writer = None            # GitHubWriter (gated); None = no GitHub writes at all
        self._handoffs: dict[str, dict] = {}
        self._drafts: dict[str, dict] = {}
        self._decisions_by_room: dict[str, list] = defaultdict(list)
        self._draft_inflight: set[str] = set()
        self._read_upto: dict[tuple, int] = {}   # (room, person) -> sequence they last read up to; memory only
        self._read_floor = 0                     # after a restart nothing older counts as unread

    def _people(self) -> dict:
        if self.people_source is not None:
            try:
                return normalize_people(self.people_source())
            except ValueError:
                return {}
        return self.people

    @staticmethod
    def _approver(pr) -> bool:
        return not pr.is_agent and bool(pr.approver or pr.admin)

    # ---------------------------------------------------------------- state fold
    def _apply_commands(self, ev: dict) -> None:
        t, p, seq = ev["event_type"], ev["payload"], ev["sequence"]
        if t in POSTED_EVENTS:
            self._idem[(p["sender_key"], p["room_id"], p["idempotency_key"])] = (p["fingerprint"], seq)
            self._msg_room[ev["event_id"]] = (p["room_id"], seq)
        if t == "handoff":
            self._handoffs[p["handoff_id"]] = {"room_id": p["room_id"], "from": p["from"], "to": p["to"],
                                               "status": "pending", "seq": seq, "created_epoch": p["created_epoch"],
                                               "by": p["sender_key"], "answered_by": None}
        elif t == "handoff_updated":
            h = self._handoffs.get(p["handoff_id"])
            if h:
                h.update(status=p["status"], answered_by=p["answered_by"])
        elif t == "decision":
            self._decisions_by_room[p["room_id"]].append(ev)
        elif t in ("issue_draft", "assignment"):
            self._drafts[p["draft"]["draft_id"]] = {**p["draft"], "room_id": p["room_id"], "status": "draft"}
        elif t == "github_written":
            d = self._drafts.get(p["github"]["draft_id"])
            if d:
                d.update(status="created" if d["kind"] == "issue" else "assigned", result=dict(p["github"]))

    # ---------------------------------------------------------------- addressing
    def _address_lookup(self, room_id: str, token: str) -> tuple[str, Any]:
        """("hit", mention) | ("ambiguous", ["@owner.harness", ...]) | ("none", None), by the room's precedence."""
        entries = self._address_entries(room_id)
        for c in mentions.candidates(token.lower()):
            if c in entries:
                v = entries[c]
                if isinstance(v, list):
                    forms = sorted({"@" + self._address(h["id"]) for h in v
                                    if h["kind"] == "agent" and self._address(h["id"])})
                    return "ambiguous", forms
                return "hit", v
        return "none", None

    def _target(self, room_id: str, token: str, what: str) -> dict:
        kind, hit = self._address_lookup(room_id, token)
        from .collab import CollabError
        if kind == "ambiguous":
            raise CollabError(400, f"More than one agent or person is called @{token}; use "
                                   f"{' or '.join(hit) if hit else '@owner.harness'}.")
        if kind == "none":
            raise CollabError(400, f"@{token} addresses nobody in this room")
        if hit["kind"] == "group":
            raise CollabError(400, f"{what} goes to one person or agent, not a group (@{hit['name']})")
        return {"kind": hit["kind"], "id": hit["id"], "name": hit["name"]}

    # ---------------------------------------------------------------- commands
    def _command_event(self, pr, room: dict, room_id: str, cmd: tuple[str, str], stored: str, *, key: str, fp: str,
                       reply, selection: list | None, first: int, catalog) -> dict:
        """Validate one command and commit its event (the caller holds the room lock and has already counted the
        post against the sender's rate limits)."""
        from .collab import CollabError
        name, rest = cmd
        if selection is not None and name != "issue":
            raise CollabError(400, "selection is only used by /issue")
        base = self._actor(pr, project_id=room["project_id"], room_id=room_id, sender_key=pr.key,
                           sender_kind=pr.kind, sender_id=pr.id, sender_external=pr.external, idempotency_key=key,
                           fingerprint=fp, text=stored, in_reply_to=reply, trust="untrusted",
                           visibility_scope="room", outcome="allowed", project_ids=[room["project_id"]],
                           resource_id=room_id, created_epoch=self._now())
        rest = redact_text(rest)
        if name == "handoff":
            return self._commit("handoff", {**base, **self._handoff_payload(pr, room_id, rest),
                                            "action": "command.handoff"})
        if name == "decide":
            if not rest:
                raise CollabError(400, "usage: /decide <what was decided>")
            return self._commit("decision", {**base, "decision": rest[:4000], "action": "command.decide",
                                             "mentions": self._resolve_mentions(room_id, rest)})
        if name == "summarize":
            return self._commit("summary_request", {**base, "focus": rest[:MAX_NOTE], "action": "command.summarize",
                                                    "mentions": self._resolve_mentions(room_id, rest)})
        if not self._approver(pr):
            raise CollabError(403, f"only approvers may use /{name}: it writes to GitHub")
        if name == "issue":
            draft = self._issue_draft(pr, room, room_id, rest, reply, selection, first)
            return self._commit("issue_draft", {**base, "draft": draft, "action": "command.issue"})
        draft = self._assignment_draft(room_id, rest, catalog)
        return self._commit("assignment", {**base, "draft": draft, "action": "command.assign"})

    def _handoff_payload(self, pr, room_id: str, rest: str) -> dict:
        from .collab import CollabError
        names, tail = [], rest
        while len(names) < 2:
            m = _LEADING_NAME.match(tail)
            if not m:
                break
            names.append(m.group(1))
            tail = tail[m.end():].lstrip()
        if not names:
            raise CollabError(400, "usage: /handoff @from @to <note>, or /handoff @to <note>")
        parties = [self._target(room_id, n, "a handoff") for n in names]
        if len(parties) == 1:
            me = ("agent" if pr.is_agent else "person", pr.id)
            hit = next((m for m in self._address_entries(room_id).values()
                        if isinstance(m, dict) and (m["kind"], m["id"]) == me), None)
            parties.insert(0, {"kind": me[0], "id": pr.id, "name": hit["name"] if hit else
                               (self._user_handle.get(pr.id) or self._agents.get(pr.id, {}).get("name") or pr.id)})
        src, dst = parties
        if (src["kind"], src["id"]) == (dst["kind"], dst["id"]):
            raise CollabError(400, "a handoff needs two different parties")
        if dst["kind"] == "agent" and (self._agents.get(dst["id"]) or {}).get("external"):
            raise CollabError(400, "a remote agent cannot take a handoff (it sees chat messages only)")
        room = self._rooms[room_id]
        return {"handoff_id": ids.new_id("handoff"), "from": src, "to": dst, "note": tail[:MAX_NOTE],
                "mentions": [dict(dst)], "wakes": self._wake_targets(pr, room, room_id, [dst])}

    def _issue_draft(self, pr, room: dict, room_id: str, rest: str, reply, selection, first: int) -> dict:
        from .collab import CollabError
        m = _REPO_ARG.match(rest)
        if not m:
            raise CollabError(400, "usage: /issue owner/repo [title], replying to or selecting the messages")
        owner, repo = m.group(1), m.group(2)
        sources = list(selection or ([] if reply in (None, False) else [reply]))
        if not sources:
            raise CollabError(400, "reply to or select the messages to turn into an issue")
        events = []
        for sid in sources:
            ev = self._message_in_room(sid, room_id, first)
            if ev is None:
                raise CollabError(400, "selection must list messages in this room")
            events.append(ev)
        parts, poster = [], self._who(pr.kind, pr.id)
        for ev in events:
            p = ev["payload"]
            quoted = "\n".join("> " + line for line in str(p["text"]).splitlines() or [""])
            parts.append(f"**@{self._who(p['sender_kind'], p['sender_id'], p.get('sender_external'))}** "
                         f"({p['created_at'][:16].replace('T', ' ')} UTC):\n\n{quoted}")
        footer = f"\n\n---\nDrafted in the collaboration room “{room['name']}” by @{poster}."
        body = redact_text("\n\n".join(parts))
        if len(body) + len(footer) > ISSUE_BODY_MAX:
            body = body[:ISSUE_BODY_MAX - len(footer) - 20].rstrip() + "\n\n… (truncated)"
        title = rest[m.end():].strip() or str(events[0]["payload"]["text"]).strip().splitlines()[0]
        title = redact_text(" ".join(title.split()))[:ISSUE_TITLE_MAX] or "Untitled"
        return {"draft_id": ids.new_id("draft"), "kind": "issue", "owner": owner, "repo": repo, "title": title,
                "body": body + footer, "sources": [ev["event_id"] for ev in events]}

    def _assignment_draft(self, room_id: str, rest: str, catalog) -> dict:
        from .collab import CollabError
        refs = list(issue_refs.tokens(rest))
        names = mentions.tokens(rest)
        person_token = next(iter(names), None)
        if len(refs) != 1 or person_token is None:
            raise CollabError(400, "usage: /assign #repo-n @person")
        if catalog is None:
            raise CollabError(503, "the GitHub cache is not ready; try /assign again shortly")
        found = self._resolve_refs(rest, True, catalog)
        if not found:
            raise CollabError(400, "that #reference is not an issue in the GitHub cache")
        person = self._target(room_id, person_token, "an assignment")
        if person["kind"] != "person":
            raise CollabError(400, "/assign assigns a person, not an agent")
        login = (self._people().get(person["name"]) or {}).get("github")
        if not login:
            raise CollabError(400, f"@{person['name']} has no GitHub login: set "
                                   f"collab.people.{person['name']}.github in the fleet YAML")
        ref = found[0]
        return {"draft_id": ids.new_id("draft"), "kind": "assignment", "owner": ref["owner"], "repo": ref["repo"],
                "number": ref["number"], "person": person, "login": login}

    def _message_in_room(self, event_id: Any, room_id: str, first: int) -> dict | None:
        where = self._msg_room.get(event_id) if isinstance(event_id, str) else None
        if where is None or where[0] != room_id or where[1] < first:
            return None
        ev = self._log[where[1] - 1]
        return ev if ev["event_type"] == "message" else None

    def _who(self, kind: str, pid: str, external: bool = False) -> str:
        if kind == "agent":
            if external:
                return "remote-agent"
            return self._address(pid) or (self._agents.get(pid) or {}).get("name") or "agent"
        return self._user_handle.get(pid) or "member"

    # ---------------------------------------------------------------- reads
    def _command_view(self, ev: dict, viewer, out: dict) -> None:
        p, t = ev["payload"], ev["event_type"]
        restricted = viewer is not None and (viewer.external or (
            not viewer.is_agent and (self._member(self._rooms.get(p["room_id"]) or {"members": {}}, viewer.id)
                                     or {}).get("role") == "guest"))
        party = (lambda m: {"kind": m["kind"], "name": m["name"]}) if restricted else dict
        if "sender_key" in p:
            out.update(sender={"type": "external" if p.get("sender_external") else p["sender_kind"],
                               "id": p["sender_id"], "owner_user_id": p["actor_user_id"]},
                       trust="untrusted", in_reply_to=p.get("in_reply_to"))
        if "mentions" in p:
            out["mentions"], mine = self._mentions_for(p, viewer)
            if mine is not None:
                out["mentions_me"] = mine
        if t == "handoff":
            h = self._handoffs.get(p["handoff_id"]) or {}
            out["handoff"] = {"handoff_id": p["handoff_id"], "from": party(p["from"]), "to": party(p["to"]),
                              "note": p["note"], **self._handoff_state(h, viewer)}
        elif t == "handoff_updated":
            out["handoff"] = {"handoff_id": p["handoff_id"], "status": p["status"], "from": party(p["from"]),
                              "to": party(p["to"])}
        elif t == "decision":
            out["text"] = p["decision"]
        elif t == "summary_request":
            out["text"] = p.get("focus") or ""
        elif t in ("issue_draft", "assignment"):
            d = dict(p["draft"])
            cur = self._drafts.get(d["draft_id"]) or {}
            d["status"] = cur.get("status", "draft")
            if cur.get("result"):
                d.update(number=cur["result"].get("number", d.get("number")), url=cur["result"].get("url"))
            if "person" in d:
                d["person"] = party(d["person"])
            if restricted:
                d.pop("login", None)
            d["can_confirm"] = bool(viewer is not None and self._approver(viewer) and d["status"] == "draft")
            out["draft"] = d
        elif t == "github_written":
            g = dict(p["github"])
            if restricted:
                g.pop("login", None)
            out["github"] = g

    def _handoff_state(self, h: dict, viewer) -> dict:
        status = h.get("status", "pending")
        if status == "pending" and self._now() - h.get("created_epoch", self._now()) >= HANDOFF_TTL:
            status = "expired"
        return {"status": status, "can_answer": bool(status == "pending" and viewer is not None
                                                     and self._is_recipient(viewer, h.get("to") or {}))}

    def _is_recipient(self, pr, to: dict) -> bool:
        if to.get("kind") == "person":
            return not pr.is_agent and pr.id == to.get("id")
        if pr.is_agent:
            return pr.id == to.get("id")
        return (self._agents.get(to.get("id")) or {}).get("owner") == pr.id

    def list_decisions(self, pr, room_id: str) -> dict:
        """The room's `/decide` records (oldest first, the last DECISIONS_SHOWN), as this reader may see them."""
        with self._lock:
            _, first = self._authorize(pr, room_id, "read", "decision.list")
            self._no_external(pr)
            feed = self._decisions_by_room.get(room_id, [])
            lo = bisect.bisect_left(feed, first, key=lambda e: e["sequence"])
            rows = feed[lo:][-DECISIONS_SHOWN:]
            return {"decisions": [{k: v for k, v in self._view(ev, viewer=pr).items() if k != "project_id"}
                                  for ev in rows]}

    # ---------------------------------------------------------------- GitHub writes
    def confirm_draft(self, pr, room_id: str, draft_id: str) -> dict:
        """An approver's explicit confirm of an `/issue` or `/assign` draft: the one place a room writes to GitHub.
        Idempotent per draft (a second confirm returns the recorded result; one in flight is refused)."""
        from ..github import GitHubError, GitHubWriteError
        from .collab import CollabError
        with self._lock:
            room, _ = self._authorize(pr, room_id, "post", "github.confirm")
            d = self._drafts.get(draft_id) if isinstance(draft_id, str) else None
            if d is None or d["room_id"] != room_id:
                raise CollabError(404, "not found")
            if not self._approver(pr):
                raise CollabError(403, "only approvers may write to GitHub")
            if d["status"] != "draft":
                return {**(d.get("result") or {}), "status": d["status"], "replayed": True}
            if draft_id in self._draft_inflight:
                raise CollabError(409, "this GitHub write is already in progress")
            writer = self.github_writer
            if writer is None or not writer.status()["enabled"]:
                raise CollabError(409, GitHubWriteError("disabled").args[0], code="github_write_disabled")
            if not writer.in_scope(d["owner"]):
                raise CollabError(403, GitHubWriteError("out_of_scope").args[0], code="github_out_of_scope")
            self._throttle("github-write:" + pr.key, GITHUB_WRITES, GITHUB_WRITE_WINDOW, "GitHub write")
            self._draft_inflight.add(draft_id)
            job = dict(d)
        try:
            if job["kind"] == "issue":
                result = writer.create_issue(job["owner"], job["repo"], job["title"], job["body"])
            else:
                writer.assign(job["owner"], job["repo"], job["number"], job["login"])
                result = {"number": job["number"],
                          "url": f"https://github.com/{job['owner']}/{job['repo']}/issues/{job['number']}"}
        except GitHubWriteError as exc:
            with self._lock:
                self._draft_inflight.discard(draft_id)
            status = {"disabled": 409, "out_of_scope": 403, "invalid": 400}.get(exc.reason, 503)
            raise CollabError(status, str(exc), code="github_write_" + exc.reason) from None
        except GitHubError as exc:
            with self._lock:
                self._draft_inflight.discard(draft_id)
            raise CollabError(502, f"GitHub refused the write: {exc}", code="github_" + exc.reason) from None
        except Exception:
            with self._lock:
                self._draft_inflight.discard(draft_id)
            raise CollabError(502, "GitHub refused the write", code="github_unavailable") from None
        with self._lock:
            try:
                g = {"draft_id": draft_id, "kind": job["kind"], "owner": job["owner"], "repo": job["repo"],
                     "number": result["number"], "url": result["url"],
                     **({"login": job["login"]} if job["kind"] == "assignment" else {})}
                self._commit("github_written", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, github=g, visibility_scope="room",
                    action="github.write", outcome="allowed", project_ids=[room["project_id"]],
                    resource_id=draft_id))
            finally:
                self._draft_inflight.discard(draft_id)
            return {**result, "status": self._drafts[draft_id]["status"], "replayed": False}

    # ---------------------------------------------------------------- C7: handoff answers
    def answer_handoff(self, pr, room_id: str, handoff_id: str, action: str) -> dict:
        """The recipient (a person, the recipient agent, or that agent's owner) accepts or declines a pending
        handoff; the answer is final and posts its own card. An expired handoff (display only) cannot be answered."""
        from .collab import CollabError
        if action not in ("accept", "decline"):
            raise CollabError(400, "answer with accept or decline")
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "handoff.answer")
            self._no_external(pr)
            h = self._handoffs.get(handoff_id) if isinstance(handoff_id, str) else None
            if h is None or h["room_id"] != room_id:
                raise CollabError(404, "not found")
            if not self._is_recipient(pr, h["to"]):
                raise CollabError(403, "only the recipient may answer a handoff")
            state = self._handoff_state(h, pr)["status"]
            if state != "pending":
                raise CollabError(409, f"this handoff is already {state}")
            self._throttle("handoff-answer:" + pr.key, HANDOFF_ANSWERS, 60.0, "handoff answer")
            status = "accepted" if action == "accept" else "declined"
            # the answer reaches who handed the work over and who posted the handoff (when that was someone else)
            kind, pid = h["by"].split(":", 1)
            back = [dict(h["from"])]
            if ("agent" if kind == "agent" else "person", pid) != (h["from"]["kind"], h["from"]["id"]):
                back += self._mention_of(room_id, kind, pid)
            self._commit("handoff_updated", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, handoff_id=handoff_id, status=status,
                **{"from": h["from"], "to": h["to"]}, answered_by={"kind": pr.kind, "id": pr.id},
                sender_key=pr.key, sender_kind=pr.kind, sender_id=pr.id, sender_external=False,
                mentions=back, visibility_scope="room", action="handoff." + action, outcome="allowed",
                project_ids=[room["project_id"]], resource_id=handoff_id))
        return {"handoff_id": handoff_id, "status": status}

    def _mention_of(self, room_id: str, kind: str, pid: str) -> list[dict]:
        """The stored mention for one principal key part ("user"/"agent", id), if it still has a name here."""
        want = ("agent" if kind == "agent" else "person", pid)
        hit = next((m for m in self._address_entries(room_id).values()
                    if isinstance(m, dict) and (m["kind"], m["id"]) == want), None)
        return [dict(hit)] if hit else []

    # ---------------------------------------------------------------- C7: presence and unread mentions
    def _working(self, room_id: str, aid: str) -> bool:
        """True while the agent holds an accepted (unexpired) task in this room."""
        return any((t := self._tasks.get(tid)) and t["room_id"] == room_id and self._task_status(t) == "accepted"
                   for tid in self._tasks_by_target.get(aid, ()))

    def _unread_mentions(self, pr, room_id: str, first: int) -> int:
        """How many events in this room addressed this person since they last read it (capped)."""
        floor = max(self._read_upto.get((room_id, pr.id), self._read_floor), first - 1)
        feed = self._by_room.get(room_id, [])
        n = 0
        lo = max(bisect.bisect_right(feed, floor, key=lambda e: e["sequence"]), len(feed) - UNREAD_SCAN)
        for ev in feed[lo:]:                 # bounded: only the newest UNREAD_SCAN events are looked at
            p = ev["payload"]
            if not p.get("mentions") or p.get("sender_key") == pr.key:
                continue
            if self._mentions_for(p, pr)[1]:
                n += 1
                if n >= UNREAD_CAP:
                    break
        return n

    def _mark_read(self, room_id: str, pr) -> None:
        if not pr.is_agent:
            self._read_upto[(room_id, pr.id)] = self._seq

    # ---------------------------------------------------------------- C7: mentions that wake an agent
    def _wake_targets(self, pr, room: dict, room_id: str, found: list[dict]) -> list[str]:
        """Which mentioned agents this post may wake (decided once, when it is posted, and stored as `wakes`).

        A wake-up runs the agent's harness, so it follows the rules `create_task` enforces: the sender needs the
        task right (a person) or `instruct` (an agent), never an external agent; an agent sender must hold an
        accepted task in this room whose depth leaves room for one more step and must not wake the agent that gave
        it that task; a target with MAX_OPEN_TASKS_PER_AGENT open tasks is not woken. Budgets as for tasks:
        TASK_RATE per sender, TASK_RATE*2 per owner and TASK_RATE per target agent, per 10 minutes. A group mention
        wakes at most WAKE_FANOUT agents."""
        from .collab import MAX_OPEN_TASKS_PER_AGENT, MAX_TASK_DEPTH, TASK_RATE, Principal
        if pr.external:
            return []
        eff = self._effective(pr, room)
        if not eff or ("instruct" if pr.is_agent else "task") not in eff[0]:
            return []
        parents = None
        if pr.is_agent:
            parents = [t for tid in self._tasks_by_target.get(pr.id, ()) if (t := self._tasks.get(tid))
                       and t["room_id"] == room_id and self._task_status(t) == "accepted"
                       and t["depth"] + 1 <= MAX_TASK_DEPTH]
            if not parents:
                return []
        wanted: list[tuple[str, bool]] = []
        for m in found:
            if m["kind"] == "agent":
                wanted.append((m["id"], False))
            elif m["kind"] == "group":
                wanted += [(t["id"], True) for t in self._group_members(room_id, m["id"]) if t["kind"] == "agent"]
        out, by_group, now = [], 0, self._now()
        for aid, via_group in wanted:
            if aid in out or aid == pr.id or (via_group and by_group >= WAKE_FANOUT):
                continue
            a = self._agents.get(aid)
            teff = self._effective(Principal("agent", aid, a["owner"]), room) if a else None
            if not teff or "execute" not in teff[0]:
                continue
            if parents is not None and all(t["by_kind"] == "agent" and t["by_id"] == aid for t in parents):
                continue                                  # never straight back to the agent that gave the work
            if sum(1 for tid in self._tasks_by_target.get(aid, ())
                   if self._task_status(self._tasks[tid]) in ("pending", "accepted")) >= MAX_OPEN_TASKS_PER_AGENT:
                continue
            budgets = (("wake:" + pr.key, TASK_RATE), ("wake-owner:" + pr.owner, TASK_RATE * 2),
                       ("wake-target:" + aid, TASK_RATE))
            live = {k: [t for t in self._hits[k] if now - t < 600.0] for k, _ in budgets}
            for k, v in live.items():
                self._hits[k] = v
            if any(len(live[k]) >= limit for k, limit in budgets):
                self.stats["wake_limited"] += 1
                continue
            for k, _ in budgets:
                self._hits[k].append(now)
            out.append(aid)
            by_group += via_group
        return out

    def _may_wake(self, p: dict, room: dict) -> bool:
        """A wake-up runs the agent's harness, so its sender needs the authority create_task needs, NOW: a person
        with the room's "task" right, an agent with "instruct"; never a remote (external) agent."""
        if p.get("sender_external"):
            return False
        return self._creator_valid({"by_kind": "user" if p.get("sender_kind") == "user" else "agent",
                                    "by_id": p.get("sender_id")}, room)

    def agent_mentions(self, pr, after: int, limit: int = 20) -> dict:
        """Messages and handoffs addressed to this agent after sequence `after`, in rooms where it holds
        `execute` (a mention runs its harness, which only the room owner's approval allows), never its own.
        `after < 0` starts now: `{"mentions": [], "cursor": <now>}` (no history replay)."""
        from .collab import CollabError
        if not pr.is_agent or pr.external:
            raise CollabError(403, "forbidden")
        with self._lock:
            now = self._seq
            if after < 0:
                return {"mentions": [], "cursor": now}
            after = max(after, now - WAKE_SCAN)        # never a full-history scan under the lock
            out, cursor = [], now
            rows = []
            for rid in sorted(self._rooms_by_agent.get(pr.id, ())):
                room = self._rooms.get(rid)
                eff = self._effective(pr, room) if room else None
                if not eff or "execute" not in eff[0]:
                    continue
                feed = self._by_room.get(rid, [])
                lo = bisect.bisect_right(feed, max(after, eff[1] - 1), key=lambda e: e["sequence"])
                rows.extend((ev, room) for ev in feed[lo:] if ev["event_type"] in ("message", "handoff"))
            rows.sort(key=lambda r: r[0]["sequence"])
            for ev, room in rows:
                p = ev["payload"]
                if (p.get("sender_key") == pr.key or pr.id not in (p.get("wakes") or ())
                        or not self._mentions_for(p, pr)[1] or not self._may_wake(p, room)):
                    continue
                if len(out) >= limit:
                    cursor = out[-1]["sequence"]
                    break
                a = self._agents.get(pr.id) or {}
                if ev["event_type"] == "handoff":
                    text = f"Handoff from @{p['from']['name']} to you: {p['note']}".rstrip(": ")
                else:
                    text = p["text"]
                out.append({"event_id": ev["event_id"], "sequence": ev["sequence"], "room_id": p["room_id"],
                            "room_name": room["name"], "kind": ev["event_type"], "text": text,
                            "from": self._who(p["sender_kind"], p["sender_id"], p.get("sender_external")),
                            "target_address": self._address(pr.id), "target_name": a.get("name"),
                            "target_aliases": list(a.get("aliases", ())), "untrusted": True})
            return {"mentions": out, "cursor": cursor}
