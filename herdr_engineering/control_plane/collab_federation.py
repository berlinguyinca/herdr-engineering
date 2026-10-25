"""Room shares for server federation (ADR-0008 phase P2, Decision 3 and Decision 5): the collab-log side.

Three kinds of event are recorded in the ordinary collab log (and nowhere else; mirrored data never enters it):

* ``federation_share_granted``: a CURRENT OWNER of a room accepted that ONE named peer may pull that room's chat.
  A revocable creation (``REVOCABLE_CREATE``), so a full log cannot make a share irrevocable;
* ``federation_share_revoked``: the pre-paid revocation (``SAFETY_EVENTS``, usable to 100 % of the log). Any current
  owner may revoke, and the kill switch never blocks it (``revoke_link`` precedent);
* ``federation_peer_enabled`` / ``federation_peer_disabled``: the audit record that the OPERATOR's YAML enabled or
  disabled a peer (written by :meth:`_fed_reconcile`, never by a request). ``enabled`` is a revocable creation and
  ``disabled`` its pre-paid revocation, so the log always has room to record a peer being switched off.

A grant is a CONSENT record only. Whether anything is exported is decided per request from the hot YAML AND this
state: the operator must list ``{peer, room}`` (``export_rooms``), the share must be active and belong to that peer,
and the owner who accepted it must still be an owner. Losing any one of them closes the export on the next request.

The export itself (the field allowlist, redaction, validity window) lives here too, because it reads the collab state
under the service lock; the HTTP side and the signing are in ``federation_export`` and ``federation_sign``.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
from typing import Any

from . import ids
from .federation import FederationPolicy, parse_federation
from .redact import redact_payload

FEDERATION_EVENTS = frozenset({"federation_share_granted", "federation_share_revoked",
                               "federation_peer_enabled", "federation_peer_disabled"})
MAX_SHARES_PER_ROOM = 16        # active shares (one per peer)
MAX_SCAN = 2000                 # feed events looked at per export request
EXPORT_TEXT_MAX = 8000

# The export schema: for each exported event type, the ONLY fields that leave this server. Everything else
# (sender ids, owner ids, emails, mentions, context references, message keys, fingerprints, the room and project ids)
# is absent by construction: an allowlist, never a denylist (ADR Decision 12). A type not listed here is never exported.
EXPORT_FIELDS = {
    "message": ("event_type", "origin_event_id", "origin_sequence", "occurred_at", "sender_kind", "sender_ref",
                "text", "in_reply_to", "trust"),
}


def _pick(allowed: tuple, src: dict) -> dict:
    return {k: src[k] for k in allowed if k in src}


class FederationMixin:
    def _init_federation(self) -> None:
        self.federation_source = None          # callable -> raw `control_plane.federation` YAML section, read per request
        self._fed_shares: dict[str, dict] = {}
        self._fed_by_room: dict[str, list] = {}
        self._fed_peers: dict[str, bool] = {}  # peer id -> enabled, as last recorded in the log

    # ----------------------------------------------------------------- config (hot, fail closed)
    def fed_policy(self) -> FederationPolicy:
        from .federation import policy_from_source
        return policy_from_source(self.federation_source)

    def _fed_parsed(self) -> FederationPolicy | None:
        """The policy ONLY when the YAML was read and valid (None while it is unreadable or invalid): the log is
        reconciled to what the operator wrote, never to a transient read failure."""
        if self.federation_source is None:
            return None
        try:
            return parse_federation(self.federation_source())
        except Exception:      # noqa: BLE001
            return None

    # ----------------------------------------------------------------- the fold
    def _apply_federation(self, ev: dict) -> bool:
        t, p, seq = ev["event_type"], ev["payload"], ev["sequence"]
        if t not in FEDERATION_EVENTS:
            return False
        if t == "federation_share_granted":
            r = {k: p[k] for k in ("room_id", "project_id", "peer_id", "granted_by")}
            r.update(id=p["share_id"], granted_seq=seq, granted_epoch=p["epoch"], granted_at=p["created_at"],
                     state="active", revoked_by=None, revoked_reason=None)
            self._fed_shares[r["id"]] = r
            self._fed_by_room.setdefault(r["room_id"], []).append(r["id"])
        elif t == "federation_share_revoked":
            r = self._fed_shares.get(p["share_id"])
            if r is not None and r["state"] == "active":
                r.update(state="revoked", revoked_by=p["revoked_by"], revoked_reason=p["reason"])
        elif t == "federation_peer_enabled":
            self._fed_peers[p["peer_id"]] = True
        elif t == "federation_peer_disabled":
            self._fed_peers[p["peer_id"]] = False
        return True

    def _fed_recount(self) -> dict:
        return {"fedshare": sum(1 for s in self._fed_shares.values() if s["state"] == "active"),
                "fedpeer": sum(1 for on in self._fed_peers.values() if on)}

    # ----------------------------------------------------------------- owner API
    def _fed_active(self, room_id: str, peer_id: str) -> dict | None:
        return next((self._fed_shares[i] for i in self._fed_by_room.get(room_id, ())
                     if self._fed_shares[i]["state"] == "active" and self._fed_shares[i]["peer_id"] == peer_id), None)

    @staticmethod
    def _fed_view(r: dict) -> dict:
        return {"share_id": r["id"], "peer": r["peer_id"], "state": r["state"], "granted_by": r["granted_by"],
                "granted_at": r["granted_at"], "revoked_by": r["revoked_by"], "revoked_reason": r["revoked_reason"]}

    def grant_share(self, pr, room_id: str, body: dict) -> tuple[int, dict]:
        from .collab import CollabError
        self._require_user(pr)
        if self.degraded:
            raise CollabError(503, "collaboration is disabled; ask the operator")
        if set(body) - {"peer"}:
            raise CollabError(400, "unknown field: only the peer id is accepted")
        peer_id = body.get("peer")
        if not isinstance(peer_id, str) or not peer_id:
            raise CollabError(400, "peer must be a peer id")
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "federation.share")      # owner only; a non-member gets 404
            self._throttle("fedshare:" + pr.key, 20, 600.0, "share")                  # also bounds the denial audit below
            policy = self.fed_policy()
            peer = policy.peer(peer_id) if policy.enabled else None
            if (peer is None or not peer.enabled or not peer.public_key or room_id not in peer.export_rooms
                    or not policy.server_id or not policy.signing_key_file):
                self.stats["policy_denied"] += 1
                self._audit_denied(pr, "federation.share", "not_listed", room_id=room_id,
                                   project_ids=[room["project_id"]])
                raise CollabError(403, "the operator configuration does not list this room for that peer",
                                  code="federation_not_listed")
            have = self._fed_active(room_id, peer_id)
            if have is not None:
                return 200, {"share": self._fed_view(have), "replayed": True}
            if sum(1 for i in self._fed_by_room.get(room_id, ()) if self._fed_shares[i]["state"] == "active") \
                    >= MAX_SHARES_PER_ROOM:
                raise CollabError(429, "this room has reached its share limit")
            sid = ids.new_id("collab")
            self._commit("federation_share_granted", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, share_id=sid, peer_id=peer_id, granted_by=pr.id,
                epoch=self._now(), visibility_scope="room", action="federation.share", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=sid))
            return 201, {"share": self._fed_view(self._fed_shares[sid])}

    def list_shares(self, pr, room_id: str) -> dict:
        """Every member who can read the room is told it is shared (and with whom): their chat leaves the server."""
        self._require_user(pr)
        with self._lock:
            self._authorize(pr, room_id, "read", "federation.list")
            on = self.fed_policy().enabled
            return {"shares": [self._fed_view(self._fed_shares[i])
                               for i in reversed(self._fed_by_room.get(room_id, ()))][:100], "enabled": on}

    def revoke_share(self, pr, room_id: str, share_id: str) -> dict:
        """Never gated by the kill switch: switching federation off must not strand a share."""
        from .collab import CollabError
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "federation.revoke")      # any CURRENT owner
            r = self._fed_shares.get(share_id) if isinstance(share_id, str) else None
            if r is None or r["room_id"] != room_id:
                raise CollabError(404, "not found")
            if r["state"] == "active":
                self._commit("federation_share_revoked", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, share_id=share_id, revoked_by=pr.id,
                    reason="revoked", epoch=self._now(), visibility_scope="room", action="federation.revoke",
                    outcome="allowed", project_ids=[room["project_id"]], resource_id=share_id))
            return {"share": self._fed_view(r)}

    # ----------------------------------------------------------------- YAML -> log
    @staticmethod
    def _fed_system(**extra) -> dict:
        return {"actor_user_id": None, "actor_agent_id": None, "actor_kind": "system", "visibility_scope": "room",
                "outcome": "allowed", "project_ids": [], **extra}

    def _fed_reconcile(self) -> None:
        try:
            self._fed_reconcile_locked()
        except Exception:      # noqa: BLE001 - a full or unavailable log must not break maintain() or a graph read
            logging.getLogger(__name__).warning("federation reconcile failed", exc_info=False)

    def _fed_reconcile_locked(self) -> None:
        """Record what the operator's YAML did, and revoke what it no longer allows. Audit and tidiness only: the
        export decides from the YAML and the state on every request, so this running late never widens anything."""
        if self.federation_source is None:
            return
        with self._lock:
            now = self._now()
            if now - getattr(self, "_fed_reconcile_at", 0.0) < 5.0:
                return
            self._fed_reconcile_at = now
            policy = self._fed_parsed()
            if policy is None:
                return
            want = {p.peer_id for p in policy.peers if p.enabled}
            for peer_id in sorted(want - {k for k, v in self._fed_peers.items() if v}):
                self._commit("federation_peer_enabled", self._fed_system(
                    peer_id=peer_id, epoch=now, action="federation.peer_enabled", resource_id=peer_id))
            for peer_id in sorted(k for k, v in self._fed_peers.items() if v and k not in want):
                self._commit("federation_peer_disabled", self._fed_system(
                    peer_id=peer_id, epoch=now, action="federation.peer_disabled", resource_id=peer_id))
            for r in [s for s in self._fed_shares.values() if s["state"] == "active"]:
                peer = policy.peer(r["peer_id"])
                room = self._rooms.get(r["room_id"])
                owner = self._member(room, r["granted_by"]) if room else None
                why = ("peer_disabled" if peer is None or not peer.enabled
                       else "unlisted" if r["room_id"] not in peer.export_rooms
                       else "owner_lapsed" if not owner or owner["role"] != "owner" or not self._ok(r["granted_by"])
                       else None)
                if why:
                    self._commit("federation_share_revoked", self._fed_system(
                        project_id=r["project_id"], room_id=r["room_id"], share_id=r["id"], revoked_by="system",
                        reason=why, epoch=now, action="federation.revoke", resource_id=r["id"]))

    # ----------------------------------------------------------------- export (the signed side calls these)
    def _fed_share_for(self, share_id: Any, peer_id: str, policy: FederationPolicy) -> tuple[dict, dict] | None:
        """``(share, room)`` when EVERY condition holds right now, else None (the caller answers one uniform 404):
        federation on, the peer enabled in YAML and listing this room, the share active and this peer's, the room
        present, and the owner who accepted still an owner."""
        sh = self._fed_shares.get(share_id) if isinstance(share_id, str) else None
        peer = policy.peer(peer_id)
        if (not policy.enabled or peer is None or not peer.enabled or sh is None or sh["state"] != "active"
                or sh["peer_id"] != peer_id or sh["room_id"] not in peer.export_rooms):
            return None
        room = self._rooms.get(sh["room_id"])
        m = self._member(room, sh["granted_by"]) if room else None
        if m is None or m["role"] != "owner" or not self._ok(sh["granted_by"]):
            return None
        return sh, room

    def federation_shares(self, peer_id: str, policy: FederationPolicy) -> dict | None:
        if self.degraded:
            return None
        with self._lock:
            out = [{"share_id": s["id"], "granted_at": s["granted_at"]} for s in self._fed_shares.values()
                   if self._fed_share_for(s["id"], peer_id, policy) is not None]
        return {"shares": sorted(out, key=lambda r: r["granted_at"])}

    @staticmethod
    def _sender_ref(share_id: str, sender_id: Any) -> str:
        """A stable pseudonym for a sender inside ONE share: lets the reader tell speakers apart without a user id,
        email or display name leaving the server (and differs per share, so it cannot be joined across shares)."""
        return "p-" + hashlib.sha256(f"{share_id}|{sender_id}".encode()).hexdigest()[:12]

    def _export_message(self, sh: dict, ev: dict) -> dict | None:
        p = ev["payload"]
        if ev["event_type"] != "message" or p.get("visibility_scope", "room") != "room":
            return None                                   # org/private/mission scope and every other type never leave
        text = p.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        reply = p.get("in_reply_to")
        where = self._msg_room.get(reply) if isinstance(reply, str) else None
        row = {"event_type": "message", "origin_event_id": ev["event_id"], "origin_sequence": ev["sequence"],
               "occurred_at": p["created_at"],
               "sender_kind": "external" if p.get("sender_external") else
               ("agent" if p.get("sender_kind") == "agent" else "person"),
               "sender_ref": self._sender_ref(sh["id"], p.get("sender_id")),
               "text": text[:EXPORT_TEXT_MAX],
               # only a reply to something that is itself exported (this room, at or after the grant)
               "in_reply_to": reply if where and where[0] == sh["room_id"] and where[1] > sh["granted_seq"] else None,
               "trust": "untrusted"}
        # start from the WHOLE payload so the allowlist, not the construction above, is what removes sender ids, owner
        # ids, mentions, context and every other field (ADR Decision 12); then redact what is left
        return redact_payload(_pick(EXPORT_FIELDS["message"], {**p, **row}))

    def federation_events(self, peer_id: str, share_id: Any, cursor: int, limit: int, max_bytes: int,
                          policy: FederationPolicy) -> dict | None:
        if self.degraded:
            return None
        with self._lock:
            found = self._fed_share_for(share_id, peer_id, policy)
            if found is None:
                return None
            sh, _room = found
            feed = self._by_room.get(sh["room_id"], [])
            floor = max(cursor, sh["granted_seq"])       # history from before the grant never leaves
            lo = bisect.bisect_right(feed, floor, key=lambda e: e["sequence"])
            window = feed[lo:lo + MAX_SCAN]
            out, used, nxt = [], 0, floor
            for ev in window:
                row = self._export_message(sh, ev)
                if row is not None:
                    size = len(json.dumps(row, separators=(",", ":")))
                    if len(out) >= limit or (out and used + size > max_bytes):
                        break
                    out.append(row)
                    used += size
                nxt = ev["sequence"]
            more = lo + len(window) < len(feed) or nxt < (window[-1]["sequence"] if window else nxt)
            return {"share_id": sh["id"], "cursor": cursor, "next_cursor": nxt, "more": more, "events": out}
