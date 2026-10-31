"""Persistent Work Graph, pure core (unified workspace P02).

Everything here is a deterministic function of the SOURCE events (and source-table rows) it is given; nothing
reads a clock, a database or a serial.  The Postgres store (``work_graph_store``) persists exactly the structures
defined here and folds them with the SAME functions, so "replay from clean" is the same fold over the same
assertions, and a parity test compares the two dumps.

Model
-----
* An **assertion** is one immutable statement a source event makes about a subject (an entity, an edge or a
  historical fact).  It carries the evidence pointer (``source_event_id``) and a total-order ``rank``
  ``(occurred_at, source, source_sequence, event_id, index)``.  Assertions are append-only; ingesting the same event
  twice, or in any order, yields the same assertion set (idempotent, order-tolerant).
* A **head** (entity / edge / fact) is the fold of a subject's assertions in rank order.  Each field is decided on its
  own (a late ``task_updated`` cannot clobber the text of an early ``task_created``), a tombstone has its own rank
  (an older late "add" cannot resurrect), and edges and facts are closed intervals, never deleted.
* Canonical ids: ``wc.urn_for(kind, native_id)`` (uuid5), so every host and every replay derives the same urn.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from . import workspace_contracts as wc

SCHEMA_VERSION = 1
MAX_TEXT = 512
VIS_ORDER = {"private": 0, "mission": 1, "room": 2, "org": 3}
ENTITY_MODES = ("upsert", "ensure", "scope", "tombstone")
EDGE_MODES = ("add", "remove")
FACT_MODES = ("open", "close")
LIST_FIELDS = ("room_ids", "mission_scope", "grantees")
SCALAR_FIELDS = ("display_name", "status", "project_id", "visibility")
# kinds whose visibility is decided by the artifact registry's own ACL gate at read time, never by room linkage
GATED_KINDS = frozenset({"artifact", "artifact_version"})
ZERO_TS = "0001-01-01T00:00:00.000000Z"


# --------------------------------------------------------------------------- small helpers
def norm_ts(value: Any) -> str:
    """Microsecond UTC ISO string (lexicographically sortable).  Never falls back to the wall clock."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ZERO_TS
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    else:
        return ZERO_TS
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def rank_key(ts: str, source: str, seq: int | None, event_key: str, idx: int) -> str:
    s = 0 if seq is None or seq < 0 else int(seq)
    return f"{ts}|{source}|{s:020d}|{event_key}|{idx:04d}"


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] if s else None


def edge_id(etype: str, from_id: str, to_id: str) -> str:
    name = "edge:" + etype + "|" + from_id + "|" + to_id
    return f"urn:herdr:edge:{uuid.uuid5(wc._NS, name)}"


def fact_id(fact_kind: str, subject_ref: str, object_id: str | None) -> str:
    name = "fact:" + fact_kind + "|" + subject_ref + "|" + (object_id or "")
    return f"urn:herdr:fact:{uuid.uuid5(wc._NS, name)}"


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- the normalized source event
def normalize_event(raw: Mapping[str, Any]) -> dict:
    """A fabric_events row / collab log entry / registry-derived event as one flat dict.

    ``event_key`` identifies the event inside the ledger.  event_id is unique per ACTOR (migration 0005), so the actor
    id is part of the key whenever the envelope has one; ``source`` falls back to the entity type (collab events carry
    no envelope source) and ``source_sequence`` to the stream ``sequence``."""
    actor = raw.get("actor")
    if isinstance(actor, str):
        try:
            actor = json.loads(actor)
        except ValueError:
            actor = None
    actor_id = actor.get("id") if isinstance(actor, Mapping) and isinstance(actor.get("id"), str) else ""
    payload = raw.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    event_id = str(raw.get("event_id") or "")
    seq = raw.get("source_sequence")
    if seq is None:
        seq = raw.get("sequence")
    entity_type = str(raw.get("entity_type") or "")
    return {
        "event_id": event_id,
        "event_key": f"{actor_id}/{event_id}" if actor_id else event_id,
        "source": str(raw.get("source") or entity_type or "unknown"),
        "source_sequence": int(seq) if isinstance(seq, int) else None,
        "occurred_at": norm_ts(raw.get("source_timestamp") or raw.get("occurred_at")),
        "entity_type": entity_type,
        "entity_id": str(raw.get("entity_id") or ""),
        "event_type": str(raw.get("event_type") or ""),
        "payload": dict(payload),
        "visibility_scope": raw.get("visibility_scope"),
        "actor_id": actor_id,
        "trace_id": raw.get("trace_id"),
        **{k: raw.get(k) for k in ("project_id", "room_id", "workspace_id", "task_id", "agent_id", "mission_id",
                                   "session_id")},
    }


# --------------------------------------------------------------------------- op builders
def _scope(vis: str | None, *, rooms: Iterable[str] = (), grantees: Iterable[str] = (), project: str | None = None,
           missions: Iterable[str] = ()) -> dict:
    out: dict[str, Any] = {}
    if vis:
        out["visibility"] = vis
    rooms = sorted({r for r in rooms if isinstance(r, str) and r})
    if rooms:
        out["room_ids"] = rooms
    grantees = sorted({g for g in grantees if isinstance(g, str) and g})
    if grantees:
        out["grantees"] = grantees
    missions = sorted({m for m in missions if isinstance(m, str) and m})
    if missions:
        out["mission_scope"] = missions
    if project:
        out["project_id"] = project
    return out


def ent(kind: str, native: str, mode: str = "upsert", *, aliases: Iterable[tuple[str, str]] = (), **fields: Any) -> dict:
    if kind not in wc.ENTITY_KINDS or mode not in ENTITY_MODES or not native:
        raise wc.ContractError(f"bad entity op {kind}/{mode}")
    clean = {k: v for k, v in fields.items() if v is not None}
    return {"t": "entity", "kind": kind, "native": str(native), "mode": mode,
            "fields": clean, "aliases": sorted({(a, str(b)) for a, b in aliases if a and b})}


def edge(etype: str, a: tuple[str, str], b: tuple[str, str], mode: str = "add", **fields: Any) -> dict:
    if etype not in wc.EDGE_ENDPOINTS or mode not in EDGE_MODES:
        raise wc.ContractError(f"bad edge op {etype}/{mode}")
    allowed_from, allowed_to = wc.EDGE_ENDPOINTS[etype]
    if a[0] not in allowed_from or b[0] not in allowed_to:
        raise wc.ContractError(f"{etype} not allowed from {a[0]} to {b[0]}")
    return {"t": "edge", "etype": etype, "from": list(a), "to": list(b), "mode": mode,
            "fields": {k: v for k, v in fields.items() if v is not None}}


def fact(fact_kind: str, subject_ref: str, obj: tuple[str, str] | None, mode: str, **fields: Any) -> dict:
    if mode not in FACT_MODES:
        raise wc.ContractError(f"bad fact op {mode}")
    return {"t": "fact", "fact_kind": fact_kind, "subject_ref": subject_ref, "object": list(obj) if obj else None,
            "mode": mode, "fields": {k: v for k, v in fields.items() if v is not None}}


# --------------------------------------------------------------------------- event -> ops
def _vis_for(ev: dict, rooms: list[str]) -> str:
    v = ev.get("visibility_scope")
    if v in VIS_ORDER:
        return v
    return "room" if rooms else "private"


def _collab_ops(ev: dict) -> list[dict]:
    t, p = ev["event_type"], ev["payload"]
    pid, rid = _text(p.get("project_id")), _text(p.get("room_id"))
    user = _text(p.get("actor_user_id"))
    ops: list[dict] = []
    room_scope = _scope("room", rooms=[rid] if rid else (), project=pid)
    if t == "project_created" and pid:
        sc = _scope("room", grantees=[f"user:{user}"] if user else (), project=pid)
        ops.append(ent("project", pid, display_name=_text(p.get("name")) or pid, status="active", **sc,
                       aliases=[("collab_project", pid)]))
        ops.append(ent("team", f"project:{pid}", display_name=f"{_text(p.get('name')) or pid} team", status="active",
                       **sc, attrs={"project_id": pid}))
        ops.append(edge("references", ("project", pid), ("team", f"project:{pid}"), **sc))
    elif t == "project_member_added" and pid and _text(p.get("subject_user_id")):
        uid = _text(p["subject_user_id"])
        ops.append(ent("project", pid, "scope", grantees=[f"user:{uid}"]))
        ops.append(ent("team", f"project:{pid}", "scope", grantees=[f"user:{uid}"]))
        ops.append(fact("team_role", f"user:{uid}", ("team", f"project:{pid}"), "open",
                        role=_text(p.get("role")), project_id=pid, **_scope(None, grantees=[f"user:{uid}"])))
    elif t == "room_created" and rid and pid:
        ops.append(ent("room", rid, display_name=_text(p.get("name")) or rid, status="active", **room_scope,
                       aliases=[("collab_room", rid)]))
        ops.append(ent("project", pid, "scope", **_scope(None, rooms=[rid], project=pid)))
        ops.append(edge("contains", ("project", pid), ("room", rid), **room_scope))
        if user:                                     # the creator is the owner; HCN emits no member_added for them
            ops.append(fact("team_role", f"user:{user}", ("room", rid), "open", role="owner", **room_scope))
    elif t in ("member_added", "member_removed") and rid and _text(p.get("subject_user_id")):
        uid = _text(p["subject_user_id"])
        sc = _scope("room", rooms=[rid], project=pid)
        if t == "member_added":
            ops.append(fact("team_role", f"user:{uid}", ("room", rid), "open", role=_text(p.get("role")),
                            expires_at=p.get("expires_at"), **sc))
        else:
            ops.append(fact("team_role", f"user:{uid}", ("room", rid), "close", **sc))
    elif t == "agent_enrolled" and _text(p.get("agent_id")):
        aid, owner = _text(p["agent_id"]), _text(p.get("owner_user_id")) or user
        rooms = [rid] if rid else []
        sc = _scope("room", rooms=rooms, grantees=[f"user:{owner}"] if owner else (), project=pid)
        ops.append(ent("agent", aid, display_name=_text(p.get("name")) or aid, status="active", **sc,
                       attrs={"harness_type": _text(p.get("harness_type"), 64), "owner_user_id": owner,
                              "instance_id": _text(p.get("instance_id"), 120), "device_id": _text(p.get("device_id"), 120),
                              "external": bool(p.get("external")) or None},
                       aliases=[("collab_agent", aid)]))
    elif t == "agent_granted" and rid and _text(p.get("agent_id")):
        aid = _text(p["agent_id"])
        sc = _scope("room", rooms=[rid], project=pid)
        ops.append(ent("agent", aid, "scope", **_scope(None, rooms=[rid])))
        ops.append(fact("agent_grant", f"agent:{aid}", ("room", rid), "open",
                        actions=sorted(p.get("actions") or []), expires_at=p.get("expires_at"), **sc))
    elif t in ("agent_grant_revoked", "agent_grant_expired") and rid and _text(p.get("agent_id")):
        aid = _text(p["agent_id"])
        ops.append(fact("agent_grant", f"agent:{aid}", ("room", rid), "close", **_scope("room", rooms=[rid], project=pid)))
    elif t == "agent_revoked" and _text(p.get("agent_id")):
        aid = _text(p["agent_id"])
        rooms = [r for r in (p.get("room_ids") or ([rid] if rid else [])) if isinstance(r, str)]
        sc = _scope("room", rooms=rooms, grantees=[f"user:{user}"] if user else (), project=pid)
        ops.append(ent("agent", aid, status="revoked", **sc))            # historical: the node stays
        for r in rooms:
            ops.append(fact("agent_grant", f"agent:{aid}", ("room", r), "close", **_scope("room", rooms=[r])))
    elif t == "task_created" and _text(p.get("task_id")) and rid:
        tid = _text(p["task_id"])
        sc = _scope("room", rooms=[rid], project=pid)
        text = _text(p.get("text"), 4000) or ""
        if p.get("target_kind") == "user":      # a task for a person: its words and its assignee stay in the collab service
            text = ""
        ops.append(ent("task", tid, display_name=(text.splitlines() or [tid])[0][:120] or tid, status="pending", **sc,
                       aliases=[("collab_task", tid)],
                       attrs={"room_task_id": tid, "room_id": rid, "target_agent_id": _text(p.get("target_agent_id")),
                              "parent_task_id": _text(p.get("parent_task_id")), "depth": p.get("depth"),
                              "created_by_kind": _text(p.get("created_by_kind"), 32),
                              "created_by_id": _text(p.get("created_by_id")), "root_user_id": _text(p.get("root_user_id")),
                              "reviews_task_id": _text(p.get("reviews_task_id")), "room_task_ttl": "24h"}))
        ops.append(edge("contains", ("room", rid), ("task", tid), **sc))
        if pid:
            ops.append(edge("contains", ("project", pid), ("task", tid), **sc))
        if _text(p.get("target_agent_id")):
            ops.append(edge("assigned_to", ("task", tid), ("agent", _text(p["target_agent_id"])), **sc))
        if _text(p.get("parent_task_id")):
            ops.append(edge("spawned", ("task", _text(p["parent_task_id"])), ("task", tid), **sc))
        if _text(p.get("reviews_task_id")):
            # a review depends on the work it reviews (dependent -> prerequisite); the attr tells it from other dependencies
            ops.append(edge("depends_on", ("task", tid), ("task", _text(p["reviews_task_id"])), **sc))
    elif t in ("task_updated", "task_progress") and _text(p.get("task_id")):
        tid = _text(p["task_id"])
        sc = _scope("room", rooms=[rid] if rid else (), project=pid)
        status = _text(p.get("status"), 64) if t == "task_updated" else None
        ops.append(ent("task", tid, status=status, **sc,
                       attrs={"last_update": t, "result_chars": len(str(p.get("result") or "")) or None}))
    elif t in ("team_attached", "team_detached") and rid and _text(p.get("team_id")):
        # Team collaboration slice 1.  A team's membership is its own data: the AGENTS the owner attached carry a
        # ``team_role`` fact on the team, the attach is a live ``references`` edge team -> room, and NOTHING here reads the
        # room's people (design contradiction 1).  No entity upsert, so an attach cannot widen the visibility of a team that
        # was made without a room.
        tid = _text(p["team_id"])
        sc = _scope("room", rooms=[rid], project=pid)
        mode = "add" if t == "team_attached" else "remove"
        ops.append(edge("references", ("team", tid), ("room", rid), mode, **sc))
        agents = p.get("agents") or {}
        for aid in sorted(agents if isinstance(agents, dict) else agents):
            aid = _text(aid)
            if not aid:
                continue
            role = _text(agents.get(aid), 64) if isinstance(agents, dict) else None
            if t == "team_attached":
                ops.append(ent("agent", aid, "scope", **_scope(None, rooms=[rid])))
                ops.append(fact("team_role", f"agent:{aid}", ("team", tid), "open", role=role, **sc))
                ops.append(fact("agent_grant", f"agent:{aid}", ("room", rid), "open",
                                actions=sorted((p.get("actions") or {}).get(aid) or []), expires_at=None, team_id=tid, **sc))
            else:
                ops.append(fact("team_role", f"agent:{aid}", ("team", tid), "close", **sc))
                ops.append(fact("agent_grant", f"agent:{aid}", ("room", rid), "close", **sc))
    elif t == "mission_linked" and rid and _text(p.get("mission_id")):
        mid = _text(p["mission_id"])
        sc = _scope("room", rooms=[rid], project=pid)
        ops.append(ent("mission", mid, "ensure", **sc))
        ops.append(edge("contains", ("room", rid), ("mission", mid), **sc))
    elif t == "mission_unlinked" and rid and _text(p.get("mission_id")):
        mid = _text(p["mission_id"])
        ops.append(edge("contains", ("room", rid), ("mission", mid), "remove", **_scope("room", rooms=[rid], project=pid)))
    return ops


_SESSION_STARTS = {"sessioncreated": "created", "session.created": "created", "sessionstarted": "active",
                   "session.started": "active", "session.resumed": "active"}
_SESSION_STOPS = {"sessionstopped": "stopped", "session.stopped": "stopped", "sessionended": "stopped",
                  "session.ended": "stopped", "session.exited": "stopped", "session.closed": "stopped"}


def _session_ops(ev: dict) -> list[dict]:
    """Pi / Herdr session events (the envelope row ``session.started`` AND the server's derived ``SessionStarted``).

    Accepted ids (docs/specs/.../contracts/PI_INGEST_FIELDS.md), each from the envelope column OR the payload, each
    optional: ``room_id``, ``workspace_id``, ``agent_id`` (the HCN agent), ``agent_instance_id`` (the Pi process),
    ``mission_id``, ``task_id``, ``project_id``.  A derived row carries no scope; it then writes NO scope fields, so it can
    neither widen nor narrow what the envelope row said.  A session no source scoped stays private to its writer."""
    t, p = ev["event_type"], ev["payload"]
    sid = ev["entity_id"] or _text(p.get("session_id"))
    if not sid:
        return []

    def pick(name):
        return _text(ev.get(name)) or _text(p.get(name))
    rid, wid, inst, agent = pick("room_id"), pick("workspace_id"), pick("agent_instance_id"), pick("agent_id")
    mid, tid, pid = pick("mission_id"), pick("task_id"), pick("project_id")
    actor = ev.get("actor_id")
    scoped = ev.get("visibility_scope") in VIS_ORDER or bool(rid)
    vis = _vis_for(ev, [rid] if rid else [])
    if vis in ("room", "mission") and not rid:
        vis = "private"
    sc = _scope(vis, rooms=[rid] if rid else (), grantees=[actor] if actor and vis == "private" else (), project=pid) if scoped else \
        _scope(None, grantees=[actor] if actor else (), project=pid)
    key = t.lower()
    status = _SESSION_STARTS.get(key) or _SESSION_STOPS.get(key) or _text(p.get("status"), 64)
    ops = [ent("agent_session", sid, status=status, display_name=_text(p.get("agent_role")), **sc,
               attrs={"workspace_id": wid, "agent_instance_id": inst, "agent_id": agent, "model": _text(p.get("model"), 128),
                      "agent_role": _text(p.get("agent_role"), 64), "room_id": rid}, aliases=[("pi_session", sid)])]
    if mid:
        ops.append(edge("spawned", ("mission", mid), ("agent_session", sid), **sc))
    if tid:
        ops.append(edge("spawned", ("task", tid), ("agent_session", sid), **sc))
    who = (("agent", agent) if agent else None)
    subject = f"agent:{agent}" if agent else (f"agent_instance:{inst}" if inst else None)
    if who:
        ops.append(ent("agent", agent, "ensure", **sc))
    if subject and (key in _SESSION_STARTS or key in _SESSION_STOPS):
        # historical fact: when an agent (instance) held a session; closed on stop, never deleted
        ops.append(fact("agent_session", subject, ("agent_session", sid), "close" if key in _SESSION_STOPS else "open",
                        workspace_id=wid, room_id=rid, agent_instance_id=inst, **sc))
    return ops


def _split_issue_ref(ref: str) -> str:
    return ref.strip()


def artifact_ops(art: Mapping[str, Any], version: Mapping[str, Any] | None, bindings: Iterable[Mapping[str, Any]],
                 *, parent_version_id: str | None = None) -> list[dict]:
    """The registry's view of one artifact version as ops (used by the event mapper AND by the reconciler, which is what
    makes both paths produce the same assertions).  Visibility is the registry's own gate, never room linkage."""
    aid = str(art["artifact_id"])
    ops = [ent("artifact", aid, display_name=_text(art.get("display_name") or art.get("original_filename")) or aid,
               status=_text(art.get("status")) or "available", visibility="org",
               attrs={"artifact_type": _text(art.get("artifact_type"), 64), "mime_type": _text(art.get("mime_type"), 128),
                      "created_by": _text(art.get("created_by")), "sha256": _text(art.get("sha256"), 80)},
               aliases=[("artifact", aid)])]
    vid = None
    if version:
        vid = str(version["version_id"])
        ops.append(ent("artifact_version", vid, display_name=f"{aid} v{version.get('version_number')}",
                       status=_text(version.get("state")) or "committed", visibility="org",
                       attrs={"artifact_id": aid, "version_number": version.get("version_number"),
                              "parent_version_id": parent_version_id or _text(version.get("parent_version_id")),
                              "created_by": _text(version.get("created_by"))},
                       aliases=[("artifact_version", vid)]))
        ops.append(edge("contains", ("artifact", aid), ("artifact_version", vid)))
        pv = parent_version_id or _text(version.get("parent_version_id"))
        if pv:
            ops.append(edge("supersedes", ("artifact_version", vid), ("artifact_version", pv)))
            ops.append(edge("derived_from", ("artifact_version", vid), ("artifact_version", pv)))
    for b in bindings:
        room, issue, task, project = (_text(b.get(k)) for k in ("room_id", "issue_ref", "task_id", "project_id"))
        mission, session = _text(b.get("mission_id")), _text(b.get("session_id"))
        # a binding that names a room makes the work objects it links visible to that room; a binding with no room
        # leaves them as they are (fail closed: nothing is widened by an artifact link)
        sc = _scope("room", rooms=[room], project=project) if room else {}
        if project:
            ops.append(edge("contains", ("project", project), ("artifact", aid), **sc))
        if room:
            ops.append(edge("contains", ("room", room), ("artifact", aid), **sc))
        if task:
            ops.append(edge("contains", ("task", task), ("artifact", aid), **sc))
            ops.append(edge("produced", ("task", task), ("artifact", aid), **sc))
        if issue:
            ops.append(edge("contains", ("issue", _split_issue_ref(issue)), ("artifact", aid), **sc))
        if mission:
            ops.append(edge("contains", ("mission", mission), ("artifact", aid), **sc))
        if session:
            ops.append(edge("produced", ("agent_session", session), ("artifact", aid), **sc))
    return ops


def _artifact_ops(ev: dict) -> list[dict]:
    p = ev["payload"]
    if not _text(p.get("artifact_id")) or not ev["event_type"].startswith("artifact"):
        return []
    art = {"artifact_id": p["artifact_id"], "display_name": p.get("title") or p.get("display_name"),
           "artifact_type": p.get("artifact_type"), "created_by": p.get("created_by"), "status": p.get("status")}
    ver = None
    if _text(p.get("version_id")):
        ver = {"version_id": p["version_id"], "version_number": p.get("version_number"), "state": p.get("state"),
               "created_by": p.get("created_by")}
    bindings = list(p.get("bindings") or [])
    for key in ("room_id", "issue_ref", "task_id", "project_id", "mission_id", "session_id"):
        if _text(p.get(key)) or _text(ev.get(key)):
            bindings.append({key: _text(p.get(key)) or _text(ev.get(key))})
    return artifact_ops(art, ver, bindings, parent_version_id=_text(p.get("parent_version_id")))


TEAM_ORIGIN = "explicit"          # attrs.origin of a team somebody created on purpose (the per-project team says "project")


def _team_ops(ev: dict) -> list[dict]:
    """Explicit teams (operator action; see ``WorkGraphStore.team_command``).  A team linked to a room is visible to that
    room's non-guest members -- the room's members ARE the team's members; one with no room is visible to operators only
    (``room`` visibility with no rooms), so a team is never named to people it does not list."""
    t, p, native = ev["event_type"], ev["payload"], ev["entity_id"]
    if not native:
        return []
    if t == "team_created":
        name, pid, rid = _text(p.get("name"), 120), _text(p.get("project_id")), _text(p.get("room_id"))
        if not name:
            return []
        sc = _scope("room", rooms=[rid] if rid else (), project=pid)
        ops = [ent("team", native, display_name=name, status="active", **sc,
                   attrs={"origin": TEAM_ORIGIN}, aliases=[("team_explicit", native)])]
        if pid:
            ops.append(edge("references", ("project", pid), ("team", native), **sc))
        if rid:
            ops.append(edge("contains", ("team", native), ("room", rid), **sc))
        return ops
    if t == "team_renamed" and _text(p.get("name"), 120):
        return [ent("team", native, display_name=_text(p.get("name"), 120))]
    if t == "team_archived":
        return [ent("team", native, status="archived")]
    return []


def ops_for_event(ev: dict) -> list[dict]:
    et = ev["entity_type"]
    if et == "collab":
        return _collab_ops(ev)
    if et == "team":
        return _team_ops(ev)
    if et == "session":
        return _session_ops(ev)
    if et == "artifact":
        return _artifact_ops(ev)
    return []


HANDLED_ENTITY_TYPES = ("collab", "session", "artifact", "team")


def assertions_for_event(ev: dict, ops: list[dict] | None = None) -> list[dict]:
    """Ops -> assertions with ranks and evidence.  Edge endpoints get ``ensure`` assertions from the same event so no edge
    ever points at a missing entity (the stub carries the edge event as its evidence)."""
    ops = ops_for_event(ev) if ops is None else ops
    out: list[dict] = []
    idx = 0

    def push(subject_kind, subject_id, mode, body):
        nonlocal idx
        out.append({"aid": f"{ev['event_key']}#{idx}", "event_key": ev["event_key"], "source": ev["source"],
                    "source_sequence": ev["source_sequence"], "occurred_at": ev["occurred_at"],
                    "rank": rank_key(ev["occurred_at"], ev["source"], ev["source_sequence"], ev["event_key"], idx),
                    "subject_kind": subject_kind, "subject_id": subject_id, "mode": mode, "body": body})
        idx += 1

    for op in ops:
        if op["t"] == "entity":
            uid = wc.urn_for(op["kind"], op["native"])
            push("entity", uid, op["mode"], {"kind": op["kind"], "native": op["native"], "fields": op["fields"],
                                             "aliases": [list(a) for a in op["aliases"]]})
        elif op["t"] == "edge":
            f_id, t_id = wc.urn_for(*op["from"]), wc.urn_for(*op["to"])
            for end in (op["from"], op["to"]):
                push("entity", wc.urn_for(*end), "ensure",
                     {"kind": end[0], "native": end[1], "fields": _ensure_scope(op["fields"]), "aliases": []})
            push("edge", edge_id(op["etype"], f_id, t_id), op["mode"],
                 {"etype": op["etype"], "from_id": f_id, "to_id": t_id, "fields": op["fields"]})
        else:
            obj = wc.urn_for(*op["object"]) if op["object"] else None
            if op["object"]:
                push("entity", obj, "ensure", {"kind": op["object"][0], "native": op["object"][1],
                                               "fields": _ensure_scope(op["fields"]), "aliases": []})
            push("fact", fact_id(op["fact_kind"], op["subject_ref"], obj), op["mode"],
                 {"fact_kind": op["fact_kind"], "subject_ref": op["subject_ref"], "object_id": obj, "fields": op["fields"]})
    return out


def _ensure_scope(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if k in ("visibility", "room_ids", "grantees", "mission_scope", "project_id")}


# --------------------------------------------------------------------------- folds
def _narrowest(values: Iterable[str]) -> str | None:
    vals = [v for v in values if v in VIS_ORDER]
    return min(vals, key=lambda v: VIS_ORDER[v]) if vals else None


def _union(ops: Iterable[dict], name: str) -> list[str]:
    s: set[str] = set()
    for a in ops:
        s.update(x for x in (a["body"].get("fields") or {}).get(name, ()) if isinstance(x, str))
    return sorted(s)


def _has_scope(a: dict) -> bool:
    f = a["body"].get("fields") or {}
    return any(f.get(k) for k in ("visibility", "room_ids", "grantees", "mission_scope"))


def fold_entity(entity_id: str, asserts: list[dict]) -> dict:
    """Head of one entity from ALL its assertions (any arrival order)."""
    ops = sorted(asserts, key=lambda a: a["rank"])
    first = ops[0]
    kind = first["body"]["kind"]
    native = first["body"]["native"]
    ups = [a for a in ops if a["mode"] == "upsert"]
    content = ups if ups else [a for a in ops if a["mode"] == "ensure"]
    # scope comes from the upserts that SAY something about scope (an enrichment upsert, e.g. a GitHub title, says nothing);
    # otherwise from the edges' ensure stubs.  ``scope`` ops (a project learning its rooms) always count.
    scoped = [a for a in ups if _has_scope(a)] or [a for a in ops if a["mode"] == "ensure"]
    scoped = scoped + [a for a in ops if a["mode"] == "scope"]
    immutable = kind in wc.IMMUTABLE_KINDS
    fields: dict[str, Any] = {}
    attrs: dict[str, Any] = {}
    for a in (reversed(content) if immutable else content):        # immutable kinds: the FIRST writer's content stands
        f = a["body"].get("fields") or {}
        for name in ("display_name", "project_id"):
            if f.get(name) is not None:
                fields[name] = f[name]
        for k, v in (f.get("attrs") or {}).items():
            if v is not None:
                attrs[k] = v
    for a in content:                                                # status is lifecycle: last writer wins even for versions
        s = (a["body"].get("fields") or {}).get("status")
        if s is not None:
            fields["status"] = s
    for a in scoped:
        if (a["body"].get("fields") or {}).get("project_id") and "project_id" not in fields:
            fields["project_id"] = a["body"]["fields"]["project_id"]
    vis = _narrowest((a["body"].get("fields") or {}).get("visibility") for a in scoped)
    if kind in GATED_KINDS:
        vis = "artifact"
    grantees = _union(scoped, "grantees")
    live = [a for a in ops if a["mode"] in ("upsert", "tombstone")]
    tomb = bool(live) and live[-1]["mode"] == "tombstone"
    aliases = sorted({tuple(x) for a in ops for x in a["body"].get("aliases", ())})
    changes = [a for a in ups]
    return {
        "entity_id": entity_id, "kind": kind, "native_id": native,
        "display_name": fields.get("display_name") or native, "status": fields.get("status") or "unknown",
        "project_id": fields.get("project_id"), "visibility": vis or "private",
        "room_ids": _union(scoped, "room_ids"), "mission_scope": _union(scoped, "mission_scope"), "grantees": grantees,
        "attrs": {k: attrs[k] for k in sorted(attrs)}, "aliases": [list(a) for a in aliases],
        "version": len(changes), "has_upsert": bool(ups),
        "created_at": ops[0]["occurred_at"], "updated_at": (changes[-1] if changes else ops[-1])["occurred_at"],
        "tombstoned_at": live[-1]["occurred_at"] if tomb else None,
        "source_event_id": first["event_key"],
    }


def _intervals(ops: list[dict], open_mode: str, close_mode: str, with_attrs: bool) -> list[list]:
    """Closed intervals from open/close ops in rank order.  A re-open while open is a no-op unless the attributes change
    (then the old segment is closed at that time and a new one opened).  A close while closed is a no-op."""
    out: list[list] = []
    for a in ops:
        attrs = a["body"].get("fields") if with_attrs else None
        attrs = {k: v for k, v in (attrs or {}).items() if k not in ("room_ids", "grantees", "mission_scope", "visibility",
                                                                       "project_id")} if with_attrs else None
        if a["mode"] == open_mode:
            if out and out[-1][1] is None:
                if with_attrs and out[-1][2] != attrs:
                    out[-1][1] = a["occurred_at"]
                    out.append([a["occurred_at"], None, attrs, a["event_key"]])
                continue
            out.append([a["occurred_at"], None, attrs, a["event_key"]])
        elif a["mode"] == close_mode and out and out[-1][1] is None:
            out[-1][1] = a["occurred_at"]
    return out


def fold_edge(eid: str, asserts: list[dict]) -> dict:
    ops = sorted(asserts, key=lambda a: a["rank"])
    iv = _intervals(ops, "add", "remove", False)
    adds = [a for a in ops if a["mode"] == "add"]
    first = adds[0] if adds else ops[0]
    b = first["body"]
    live = bool(iv) and iv[-1][1] is None
    f_all = [a["body"].get("fields") or {} for a in ops]
    return {
        "edge_id": eid, "edge_type": b["etype"], "from_id": b["from_id"], "to_id": b["to_id"], "live": live,
        "intervals": [[x[0], x[1]] for x in iv], "valid_from": iv[0][0] if iv else None,
        "tombstoned_at": iv[-1][1] if iv and iv[-1][1] else None,
        "room_ids": sorted({r for f in f_all for r in f.get("room_ids", ())}),
        "project_id": next((f["project_id"] for f in f_all if f.get("project_id")), None),
        "source_event_id": first["event_key"],
    }


def fold_fact(fid: str, asserts: list[dict]) -> dict:
    ops = sorted(asserts, key=lambda a: a["rank"])
    iv = _intervals(ops, "open", "close", True)
    first = next((a for a in ops if a["mode"] == "open"), ops[0])
    b = first["body"]
    f_all = [a["body"].get("fields") or {} for a in ops]
    return {
        "fact_id": fid, "fact_kind": b["fact_kind"], "subject_ref": b["subject_ref"], "object_id": b["object_id"],
        "segments": [{"from": x[0], "to": x[1], "attrs": x[2] or {}, "source_event_id": x[3]} for x in iv],
        "open": bool(iv) and iv[-1][1] is None,
        "room_ids": sorted({r for f in f_all for r in f.get("room_ids", ())}),
        "grantees": sorted({g for f in f_all for g in f.get("grantees", ())}),
        "project_id": next((f["project_id"] for f in f_all if f.get("project_id")), None),
        "source_event_id": first["event_key"],
    }


FOLDS = {"entity": fold_entity, "edge": fold_edge, "fact": fold_fact}


# --------------------------------------------------------------------------- visibility (the one rule)
def visible(principal: Mapping[str, Any] | None, row: Mapping[str, Any]) -> bool | None:
    """Can ``principal`` see an entity row?  True / False, or ``None`` = "ask the artifact registry's gate" (kind artifact).

    Mirrors ``graph.can_see``: unauthenticated -> no; ``org`` -> any fleet KEY (never a signed-in person); room/mission ->
    an operator, or a member whose collab ``access`` snapshot covers one of the row's rooms (mission: the (room, mission)
    pair); ``private`` -> only a named grantee (operators included: nobody else).  A named grantee sees the row whatever
    its scope (the owner of a project sees it before it has a room)."""
    if not principal or principal.get("authenticated_by") in (None, "none") or principal.get("kind") in (None, "unknown"):
        return False
    pid = principal.get("id")
    if pid and pid in (row.get("grantees") or ()):
        return True
    vis = row.get("visibility")
    kind = principal.get("kind")
    if vis == "artifact":
        return None
    if vis == "org":
        return kind != "member"
    if vis in ("room", "mission"):
        if kind == "operator":
            return True
        access = principal.get("access")
        if kind != "member" or principal.get("authenticated_by") != "collab" or not isinstance(access, Mapping):
            return False
        if vis == "room":
            return any(r in access.get("rooms", ()) for r in row.get("room_ids") or ())
        pairs = access.get("room_missions", ())
        return any(tuple(x.split("|", 1)) in pairs for x in row.get("mission_scope") or ())
    return False


# --------------------------------------------------------------------------- the in-memory projection
class Projection:
    """The whole projection in memory.  ``apply`` is idempotent per event, order-tolerant and retry-safe; ``dump`` is the
    canonical comparison form (no serials, no wall clock)."""

    def __init__(self) -> None:
        self.applied: dict[str, str] = {}                     # event_key -> source
        self.assertions: dict[str, dict] = {}
        self._by_subject: dict[tuple[str, str], list[dict]] = {}
        self.rejected: dict[str, str] = {}

    def apply(self, raw: Mapping[str, Any]) -> bool:
        """True when the event changed the projection, False for a repeat (or an event this projection ignores)."""
        ev = normalize_event(raw)
        if not ev["event_id"]:
            return False
        if ev["event_key"] in self.applied:
            return False
        try:
            asserts = assertions_for_event(ev)
        except wc.ContractError as exc:
            self.rejected[ev["event_key"]] = str(exc)
            asserts = []
        self.applied[ev["event_key"]] = ev["source"]
        for a in asserts:
            if a["aid"] in self.assertions:
                continue
            self.assertions[a["aid"]] = a
            self._by_subject.setdefault((a["subject_kind"], a["subject_id"]), []).append(a)
        return True

    def apply_all(self, events: Iterable[Mapping[str, Any]]) -> int:
        return sum(1 for e in events if self.apply(e))

    def heads(self, subject_kind: str) -> dict[str, dict]:
        fold = FOLDS[subject_kind]
        return {sid: fold(sid, a) for (k, sid), a in sorted(self._by_subject.items()) if k == subject_kind}

    def dump(self) -> dict:
        return {"schema": SCHEMA_VERSION, "entities": self.heads("entity"), "edges": self.heads("edge"),
                "facts": self.heads("fact")}

    def digest(self) -> str:
        return hashlib.sha256(_canon(self.dump()).encode()).hexdigest()


def dump_digest(dump: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canon(dump).encode()).hexdigest()


# --------------------------------------------------------------------------- integrity checks (gates)
def evidence_violations(dump: Mapping[str, Any], known_events: Iterable[str]) -> list[str]:
    """Every node/edge/fact must name a source event that exists (gate: "all graph nodes/edges link to source evidence")."""
    known = set(known_events)
    bad = []
    for section, idk in (("entities", "entity_id"), ("edges", "edge_id"), ("facts", "fact_id")):
        for sid, row in dump[section].items():
            if not row.get("source_event_id") or row["source_event_id"] not in known:
                bad.append(f"{section}:{sid}")
    return bad


def artifact_orphans(dump: Mapping[str, Any], registry: Mapping[str, Iterable[Mapping[str, Any]]]) -> dict[str, list[str]]:
    """Orphans in BOTH directions.  ``registry`` maps artifact_id -> its bindings (the source of truth).

    * ``missing``: a registry artifact with no node in the graph.
    * ``unlinked``: an artifact node lacking the edge a binding implies (project/room/task/issue/mission contains it).
    * ``dangling``: an artifact node whose registry artifact does not exist.
    * ``unbound``: registry artifacts with NO binding at all (nothing to link to; reported, not an error)."""
    ents = dump["entities"]
    live = {(e["edge_type"], e["from_id"], e["to_id"]) for e in dump["edges"].values() if e["live"]}
    missing, unlinked, unbound = [], [], []
    for aid, bindings in registry.items():
        uid = wc.urn_for("artifact", aid)
        if uid not in ents:
            missing.append(aid)
            continue
        bl = list(bindings)
        if not bl:
            unbound.append(aid)
        for b in bl:
            need = []
            if _text(b.get("project_id")):
                need.append(("project", b["project_id"]))
            if _text(b.get("room_id")):
                need.append(("room", b["room_id"]))
            if _text(b.get("task_id")):
                need.append(("task", b["task_id"]))
            if _text(b.get("issue_ref")):
                need.append(("issue", _split_issue_ref(b["issue_ref"])))
            if _text(b.get("mission_id")):
                need.append(("mission", b["mission_id"]))
            for kind, native in need:
                if ("contains", wc.urn_for(kind, native), uid) not in live:
                    unlinked.append(f"{aid}<-{kind}:{native}")
    known = {wc.urn_for("artifact", a) for a in registry}
    dangling = sorted(e["native_id"] for u, e in ents.items() if e["kind"] == "artifact" and u not in known)
    return {"missing": sorted(missing), "unlinked": sorted(unlinked), "dangling": dangling, "unbound": sorted(unbound)}
