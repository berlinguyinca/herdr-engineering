"""Durable handoff projection shared by local outboxes and the control plane."""
from __future__ import annotations

import copy
import hashlib
import json
import re

_HARNESS = {"pi", "claude", "codex", "opencode"}
_TRANSITIONS = {
    "queued": {"claimed", "cancelled", "expired", "failed"},
    "claimed": {"accepted", "rejected", "queued", "cancelled", "expired", "failed"},
    "accepted": {"in_progress", "rejected", "cancelled", "failed"},
    "in_progress": {"completed", "failed", "cancelled"},
    "failed": {"queued", "cancelled"},
}
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class HandoffRegistry:
    def __init__(self, repo, *, now=None):
        self.repo = repo
        self.now = now or repo._stamp
        self._rows = {}
        self._restore()

    def _restore(self):
        """Rebuild the projection from the repo's durable event projection."""
        for event in reversed(self.repo.recent_events(limit=100000)):
            if event.get("entity_type") != "handoff":
                continue
            hid = event.get("entity_id")
            payload = event.get("payload") or {}
            if event.get("event_type") == "HandoffQueued":
                self._rows[hid] = copy.deepcopy(payload)
            elif event.get("event_type") == "HandoffStateChanged" and hid in self._rows:
                row = self._rows[hid]
                row["state"] = payload.get("to", row["state"])
                row["updated_at"] = event.get("source_timestamp", row.get("updated_at"))
                row.setdefault("history", []).append({"state": row["state"],
                                                       "actor": payload.get("actor"),
                                                       "at": row["updated_at"]})

    def create(self, body, actor):
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
        self._rows[hid] = row
        self.repo.record_event("handoff", hid, "HandoffQueued", row,
                               mission_id=row.get("mission_id"), session_id=row["source"].get("session_id"))
        return copy.deepcopy(row), False

    def get(self, handoff_id):
        return copy.deepcopy(self._rows.get(handoff_id))

    def list(self, *, state=None, limit=200):
        rows = [row for row in self._rows.values() if state is None or row["state"] == state]
        return copy.deepcopy(sorted(rows, key=lambda row: row["updated_at"], reverse=True)[:limit])

    def transition(self, handoff_id, state, actor):
        row = self._rows.get(handoff_id)
        if row is None:
            raise KeyError(handoff_id)
        old = row["state"]
        if state not in _TRANSITIONS.get(old, set()):
            raise ValueError(f"invalid transition: {old} -> {state}")
        now = self.now()
        row["state"] = state
        row["updated_at"] = now
        row["history"].append({"state": state, "actor": actor, "at": now})
        self.repo.record_event("handoff", handoff_id, "HandoffStateChanged", {
            "from": old, "to": state, "actor": actor,
        }, mission_id=row.get("mission_id"), session_id=row["source"].get("session_id"))
        return copy.deepcopy(row)

    def tree(self):
        nodes = {}
        edges = []
        for row in self._rows.values():
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
