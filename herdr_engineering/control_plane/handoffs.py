"""Durable handoff projection shared by local outboxes and the control plane."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

_HARNESS = {"pi", "claude", "codex", "opencode"}
_TRANSITIONS = {
    "queued": {"claimed", "cancelled", "expired", "failed"},
    "claimed": {"accepted", "rejected", "queued", "cancelled", "expired", "failed"},
    "accepted": {"in_progress", "rejected", "cancelled", "failed"},
    "in_progress": {"completed", "failed", "cancelled"},
    "failed": {"queued", "cancelled"},
}
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
# A handoff needs a human when it ended badly, or when nobody has picked it up for this long.
ATTENTION_STATES = {"failed", "expired", "rejected"}
_PENDING = {"queued", "claimed"}
STALL_AFTER = timedelta(minutes=15)
PAGE_MAX = 200
OVERVIEW_WORKSPACES = 100
OVERVIEW_ATTENTION = 20
CHAIN_MAX = 10
UNASSIGNED = "Unassigned"


def _when(text):
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class HandoffRegistry:
    def __init__(self, repo, *, now=None):
        self.repo = repo
        self.now = now or repo._stamp
        self._rows = {}
        self._restore()

    def _restore(self):
        """Replay complete handoff streams without copying global event history."""
        rows = {}
        with self.repo._event_lock:
            for (entity_type, hid), stream in self.repo._events.items():
                if entity_type != "handoff":
                    continue
                for event in sorted(stream, key=lambda item: item.get("sequence", 0)):
                    payload = event.get("payload") or {}
                    if event.get("event_type") == "HandoffQueued":
                        row = copy.deepcopy(payload)
                        # Older queued payloads could alias the mutable live row.
                        # Seed only creation; persisted transitions rebuild history.
                        row["state"] = "queued"
                        row["updated_at"] = row.get("created_at") or event.get("source_timestamp")
                        row["history"] = [{"state": "queued", "actor": row.get("created_by"),
                                           "at": row["updated_at"]}]
                        rows[hid] = row
                    elif event.get("event_type") == "HandoffStateChanged" and hid in rows:
                        row = rows[hid]
                        row["state"] = payload.get("to", row["state"])
                        row["updated_at"] = event.get("source_timestamp", row.get("updated_at"))
                        row["history"].append({"state": row["state"],
                                               "actor": copy.deepcopy(payload.get("actor")),
                                               "at": row["updated_at"]})
            self._rows = rows

    def create(self, body, actor):
        with self.repo._event_lock:
            if not isinstance(body, dict):
                raise ValueError("handoff body must be an object")
            hid = str(body.get("handoff_id") or "")
            if not _ID.fullmatch(hid):
                raise ValueError("handoff_id is required and must be safe")
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if hid in self._rows:
                if self._rows[hid].get("fingerprint") == fingerprint:
                    return copy.deepcopy(self._rows[hid]), True
                raise ValueError("handoff_id already exists with different content")
            source = str(body.get("source_harness") or "")
            target = str(body.get("target_harness") or "")
            if source not in _HARNESS or target not in _HARNESS:
                raise ValueError("source_harness and target_harness must be supported harnesses")
            goal = body.get("goal")
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
                raise ValueError("goal must be a non-empty bounded string")
            now = self.now()
            row = {
                "handoff_id": hid, "schema_version": 1, "state": "queued",
                "created_at": now, "updated_at": now, "created_by": actor,
                "source": {"harness": source, "agent_id": body.get("source_agent_id"),
                           "session_id": body.get("source_session_id")},
                "target": {"harness": target, "agent_id": body.get("target_agent_id"),
                           "session_id": body.get("target_session_id"),
                           "model_hint": body.get("model_hint")},
                "goal": goal, "completed": list(body.get("completed") or [])[:200],
                "remaining": list(body.get("remaining") or [])[:200],
                "decisions": list(body.get("decisions") or [])[:200],
                "risks": list(body.get("risks") or [])[:200],
                "validation": list(body.get("validation") or [])[:200],
                "provenance": dict(body.get("provenance") or {}),
                "artifact_refs": list(body.get("artifact_refs") or [])[:100],
                "mission_id": body.get("mission_id"), "room_id": body.get("room_id"),
                "idempotency_key": body.get("idempotency_key"),
                "fingerprint": fingerprint,
                "history": [{"state": "queued", "actor": actor, "at": now}],
            }
            row = copy.deepcopy(row)
            self.repo.record_event("handoff", hid, "HandoffQueued", copy.deepcopy(row),
                                   mission_id=row.get("mission_id"), session_id=row["source"].get("session_id"),
                                   require_durable=self.repo._sink is not None)
            self._rows[hid] = row
            return copy.deepcopy(row), False

    def get(self, handoff_id):
        with self.repo._event_lock:
            return copy.deepcopy(self._rows.get(handoff_id))

    def list(self, *, state=None, limit=200):
        with self.repo._event_lock:
            rows = [row for row in self._rows.values() if state is None or row["state"] == state]
            return copy.deepcopy(sorted(rows, key=lambda row: row["updated_at"], reverse=True)[:limit])

    def transition(self, handoff_id, state, actor):
        with self.repo._event_lock:
            row = copy.deepcopy(self._rows.get(handoff_id))
            if row is None:
                raise KeyError(handoff_id)
            old = row["state"]
            if state not in _TRANSITIONS.get(old, set()):
                raise ValueError(f"invalid transition: {old} -> {state}")
            now = self.now()
            row["state"] = state
            row["updated_at"] = now
            row["history"].append({"state": state, "actor": copy.deepcopy(actor), "at": now})
            self.repo.record_event("handoff", handoff_id, "HandoffStateChanged", {
                "from": old, "to": state, "actor": copy.deepcopy(actor),
            }, mission_id=row.get("mission_id"), session_id=row["source"].get("session_id"),
                require_durable=self.repo._sink is not None)
            self._rows[handoff_id] = row
            return copy.deepcopy(row)

    def tree(self, session_id=None):
        """Every handoff as nodes and edges; with ``session_id`` only those touching that session, newest ``PAGE_MAX``."""
        with self.repo._event_lock:
            nodes = {}
            edges = []
            rows = self._rows.values()
            if session_id:
                rows = sorted((r for r in rows if session_id in ((r.get("source") or {}).get("session_id"), (r.get("target") or {}).get("session_id"))),
                              key=lambda r: (r.get("updated_at") or "", r["handoff_id"]), reverse=True)[:PAGE_MAX]
            for row in rows:
                source = self._node(nodes, row["source"], "source")
                target = self._node(nodes, row["target"], "target")
                edges.append({"handoff_id": row["handoff_id"], "from": source["id"],
                              "to": target["id"], "state": row["state"],
                              "goal": row["goal"][:240], "updated_at": row["updated_at"]})
            return {"nodes": list(nodes.values()), "edges": edges, "untrusted_text": True}

    @staticmethod
    def _node(nodes, descriptor, role):
        key = descriptor.get("agent_id") or descriptor.get("session_id") or f"unknown:{role}"
        node_id = f"handoff:{role}:{key}"
        nodes.setdefault(node_id, {"id": node_id, "role": role,
                                   "agent_id": descriptor.get("agent_id"),
                                   "session_id": descriptor.get("session_id"),
                                   "harness": descriptor.get("harness"),
                                   "model_hint": descriptor.get("model_hint")})
        return nodes[node_id]

    # ---- read side for dashboards: bounded by construction, so hundreds of agents stay one screen -------------------

    def _workspace(self, row):
        """The group a handoff belongs to: its source session's repository, else working directory, else its mission."""
        sid = (row.get("source") or {}).get("session_id")
        session = self.repo.get_session(sid) if sid else None
        if session:
            if session.get("repository_id"):
                return str(session["repository_id"])
            if session.get("cwd"):
                return os.path.basename(str(session["cwd"]).rstrip("/")) or UNASSIGNED
        mission = self.repo.get_mission(row["mission_id"]) if row.get("mission_id") else None
        if mission and mission.get("title"):
            return str(mission["title"])
        return UNASSIGNED

    def _stalled(self, row, now):
        updated = _when(row.get("updated_at"))
        return row["state"] in _PENDING and updated is not None and now - updated > STALL_AFTER

    def _needs_attention(self, row, now):
        return row["state"] in ATTENTION_STATES or self._stalled(row, now)

    def _summary(self, row, workspace, now):
        source, target = row.get("source") or {}, row.get("target") or {}
        return {"handoff_id": row["handoff_id"], "state": row["state"], "workspace": workspace,
                "source": {k: source.get(k) for k in ("harness", "agent_id", "session_id")},
                "target": {k: target.get(k) for k in ("harness", "agent_id", "session_id", "model_hint")},
                "goal": str(row.get("goal") or "")[:240], "mission_id": row.get("mission_id"),
                "updated_at": row.get("updated_at"), "created_at": row.get("created_at"),
                "completed": len(row.get("completed") or []), "remaining": len(row.get("remaining") or []),
                "attention": self._needs_attention(row, now), "stalled": self._stalled(row, now)}

    def overview(self):
        """Counts by workspace and the few handoffs that need a human; no rows beyond OVERVIEW_ATTENTION."""
        with self.repo._event_lock:
            now = _when(self.now()) or datetime.now(timezone.utc)
            groups, attention, states = {}, [], {}
            for row in self._rows.values():
                ws = self._workspace(row)
                g = groups.setdefault(ws, {"workspace": ws, "total": 0, "states": {}, "attention": 0, "stalled": 0, "latest_at": ""})
                g["total"] += 1
                g["states"][row["state"]] = g["states"].get(row["state"], 0) + 1
                states[row["state"]] = states.get(row["state"], 0) + 1
                g["latest_at"] = max(g["latest_at"], row.get("updated_at") or "")
                if self._stalled(row, now):
                    g["stalled"] += 1
                if self._needs_attention(row, now):
                    g["attention"] += 1
                    attention.append((row.get("updated_at") or "", row["handoff_id"], ws, row))
            ordered = sorted(groups.values(), key=lambda g: (g["attention"], g["latest_at"]), reverse=True)
            attention.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return {"total": len(self._rows), "states": states,
                    "attention_total": sum(g["attention"] for g in groups.values()),
                    "workspaces": ordered[:OVERVIEW_WORKSPACES],
                    "workspaces_omitted": max(0, len(ordered) - OVERVIEW_WORKSPACES),
                    "attention": [self._summary(r, ws, now) for _, _, ws, r in attention[:OVERVIEW_ATTENTION]],
                    "untrusted_text": True}

    def page(self, *, state=None, workspace=None, attention=False, cursor=None, limit=50):
        """Newest first, keyed on (updated_at, handoff_id) so equal timestamps never skip or repeat a row."""
        limit = max(1, min(int(limit), PAGE_MAX))
        with self.repo._event_lock:
            now = _when(self.now()) or datetime.now(timezone.utc)
            after = None
            if cursor:
                stamp, _, hid = str(cursor).partition("|")
                after = (stamp, hid)
            keyed = []
            for row in self._rows.values():
                if state and row["state"] != state:
                    continue
                if attention and not self._needs_attention(row, now):
                    continue
                key = (row.get("updated_at") or "", row["handoff_id"])
                if after is not None and key >= after:
                    continue
                keyed.append((key, row))
            keyed.sort(key=lambda t: t[0], reverse=True)
            out, more = [], False
            for key, row in keyed:
                ws = self._workspace(row)
                if workspace and ws != workspace:
                    continue
                if len(out) == limit:
                    more = True
                    break
                out.append((key, self._summary(row, ws, now)))
            return {"handoffs": [r for _, r in out],
                    "next_cursor": f"{out[-1][0][0]}|{out[-1][0][1]}" if more and out else None,
                    "untrusted_text": True}

    def chain(self, handoff_id):
        """The single path of handoffs through this one: backwards to the handoff that delivered the work to its source session,
        forwards to the one that took it on from its target session (the same session ids the Usage tab joins on). A session that
        received or gave many handoffs contributes only the nearest one in time, so the chain stays a path, not a blob."""
        with self.repo._event_lock:
            row = self._rows.get(handoff_id)
            if row is None:
                raise KeyError(handoff_id)
            now = _when(self.now()) or datetime.now(timezone.utc)
            key = lambda r: (r.get("created_at") or "", r["handoff_id"])
            by_target, by_source = {}, {}
            for r in self._rows.values():
                t, sr = (r.get("target") or {}).get("session_id"), (r.get("source") or {}).get("session_id")
                if t:
                    by_target.setdefault(t, []).append(r)
                if sr:
                    by_source.setdefault(sr, []).append(r)

            def step(current, index, side, later):
                sid = (current.get(side) or {}).get("session_id")
                pool = [r for r in index.get(sid, []) if (key(r) > key(current)) == later and r["handoff_id"] != current["handoff_id"]
                        and (not current.get("mission_id") or r.get("mission_id") in (None, current["mission_id"]))] if sid else []
                return (min if later else max)(pool, key=key) if pool else None

            def walk(index, side, later, taken):
                found, current, more = [], row, False
                while True:
                    nxt = step(current, index, side, later)
                    if nxt is None or nxt["handoff_id"] in taken:
                        break
                    if len(found) == CHAIN_MAX:
                        more = True
                        break
                    taken.add(nxt["handoff_id"])
                    found.append(nxt)
                    current = nxt
                return found, more

            taken = {handoff_id}
            before, before_more = walk(by_target, "source", False, taken)
            after, after_more = walk(by_source, "target", True, taken)
            ordered = list(reversed(before)) + [row] + after
            return {"handoff_id": handoff_id, "position": len(before), "more_before": before_more, "more_after": after_more,
                    "chain": [self._summary(r, self._workspace(r), now) for r in ordered], "untrusted_text": True}
